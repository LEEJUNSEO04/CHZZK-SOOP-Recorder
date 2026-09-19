import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pymysql


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "logs" / "unified_merge_worker.log"
RECEIPT_DIR = BASE_DIR / "logs" / "merge_receipts"
_LOG_LOCK = threading.Lock()
_LOG_THROTTLE_LOCK = threading.Lock()
_LOG_THROTTLE = {}
_INSTANCE_MUTEX = None

APP_NAME = "CHZZK Unified Merge Worker v2.0"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, icon="ℹ️"):
    line = f"[{now()}] {icon} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        try:
            sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass
    try:
        with _LOG_LOCK:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > 20 * 1024 * 1024:
                rotated = LOG_PATH.with_suffix(".log.1")
                rotated.unlink(missing_ok=True)
                LOG_PATH.replace(rotated)
            with LOG_PATH.open("a", encoding="utf-8") as fp:
                fp.write(line + "\n")
    except Exception:
        # 로그 파일 문제 때문에 병합 자체를 중단하지 않습니다.
        pass


def log_throttled(key, msg, icon="ℹ️", interval_sec=3600):
    """Avoid flooding the supervisor console with an unchanged wait reason.

    The merge worker still checks the session every scan.  Only the repeated
    informational line is suppressed, so a recovered session is processed as
    soon as its safety condition changes.
    """
    current = time.monotonic()
    with _LOG_THROTTLE_LOCK:
        previous = _LOG_THROTTLE.get(key, 0.0)
        if current - previous < max(1, int(interval_sec)):
            return False
        _LOG_THROTTLE[key] = current
        if len(_LOG_THROTTLE) > 2048:
            cutoff = current - max(1, int(interval_sec)) * 2
            stale = [item_key for item_key, seen_at in _LOG_THROTTLE.items() if seen_at < cutoff]
            for item_key in stale:
                _LOG_THROTTLE.pop(item_key, None)
    log(msg, icon)
    return True


def acquire_single_instance():
    """Acquire a process-scoped Windows mutex only when this file runs as a worker.

    recorder_v07 imports ffmpeg_concat from this module, so the mutex must never
    be acquired at import time.
    """
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\CHZZK_UNIFIED_MERGE_WORKER_V2"
    )
    if not handle:
        raise OSError("merge worker mutex creation failed")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    _INSTANCE_MUTEX = handle
    return True


def _acquire_merge_operation(timeout_ms=90 * 1000):
    """Serialize merges without freezing a live recorder behind a long repair.

    The background worker scans again later, so a busy merge engine is not an
    error that justifies waiting for hours.  A short timeout lets the recorder
    release its in-memory active slot and resume a continuing broadcast.
    """
    if os.name != "nt":
        return True
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\CHZZK_MEDIA_MERGE_ENGINE_V2"
    )
    if not handle:
        return None
    wait = ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout_ms))
    if wait not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
        ctypes.windll.kernel32.CloseHandle(handle)
        return None
    return handle


def _release_merge_operation(handle):
    if os.name != "nt" or handle is True or not handle:
        return
    import ctypes
    ctypes.windll.kernel32.ReleaseMutex(handle)
    ctypes.windll.kernel32.CloseHandle(handle)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def sanitize(text):
    text = str(text or "").strip()
    for ch in '<>:"/\\|?*\n\r\t':
        text = text.replace(ch, "_")
    return re.sub(r"\s+", "_", text).strip(" ._") or "unknown"


def normalize_name(text):
    return sanitize(text).lower()


def read_streamers():
    p = BASE_DIR / "streamers.txt"
    out = {}
    if not p.exists():
        return out

    for line in p.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [x.strip() for x in raw.split(",")]
        if len(parts) >= 4 and parts[0] in ["0", "1"]:
            name, url, quality = parts[1], parts[2], parts[3] or "best"
        elif len(parts) >= 2:
            name, url = parts[0], parts[1]
            quality = parts[2] if len(parts) >= 3 and parts[2] else "best"
        else:
            continue
        out[name] = {"name": name, "url": url, "quality": quality}
    return out


def db_connect(cfg):
    db = cfg["database"]
    return pymysql.connect(
        host=db.get("host", "127.0.0.1"),
        port=int(db.get("port", 3306)),
        user=db.get("user", "recorder"),
        password=db.get("password", ""),
        database=db.get("database", "chzzk_recorder"),
        charset=db.get("charset", "utf8mb4"),
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def recording_exists(conn, session_id):
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM recordings WHERE session_id=%s LIMIT 1", (session_id,))
        return cur.fetchone() is not None


def insert_recording(conn, data):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO recordings
            (streamer_name, broadcast_title, source_url, quality, started_at, ended_at, status,
             temp_path, final_path, file_size_mb, error_message, youtube_status,
             parts_dir, parts_count, session_id)
            VALUES (%s, %s, %s, %s, %s, NOW(), 'done',
                    '', %s, %s, %s, 'pending',
                    %s, %s, %s)
            """,
            (
                data["streamer_name"],
                data.get("broadcast_title"),
                data.get("source_url", ""),
                data.get("quality", "best"),
                data["started_at"],
                data["final_path"],
                data["file_size_mb"],
                data.get("error_message"),
                data["parts_dir"],
                data["parts_count"],
                data["session_id"],
            ),
        )
        return cur.lastrowid


def mark_source_recordings_not_requested(conn, source_session_ids, stitched_session_id):
    # The newly-created stitched row can also appear in a later stitch group.
    # Never retire the output row itself when marking its source sessions.
    stitched_id = str(stitched_session_id or "")
    ids = [str(x) for x in source_session_ids if x and str(x) != stitched_id]
    if not ids:
        return 0
    placeholders = ",".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE recordings
            SET youtube_status='not_requested',
                error_message=LEFT(CONCAT(IFNULL(error_message,''),
                    CASE WHEN IFNULL(error_message,'')='' THEN '' ELSE ' | ' END,
                    'auto-stitched into ', %s), 1000)
            WHERE session_id IN ({placeholders})
              AND youtube_status IN ('pending','failed','not_ready')
            """,
            [stitched_session_id] + ids,
        )
        return cur.rowcount


def mark_source_sessions_stitched(conn, source_session_ids, stitched_final_path):
    ids = [str(x) for x in source_session_ids if x]
    if not ids:
        return 0
    placeholders = ",".join(["%s"] * len(ids))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE recording_sessions
                SET status='stitched',
                    final_path=%s,
                    ended_at=COALESCE(ended_at, NOW())
                WHERE session_id IN ({placeholders})
                  AND status NOT IN ('recording','merging')
                """,
                [str(stitched_final_path)] + ids,
            )
            return cur.rowcount
    except Exception:
        # 구버전 DB거나 컬럼 차이가 있으면 무시합니다.
        return 0


def find_streamer_info(streamers, streamer_name):
    if streamer_name in streamers:
        return streamers[streamer_name]

    target = normalize_name(streamer_name)
    for name, info in streamers.items():
        if normalize_name(name) == target:
            return info
    return None


def stream_is_live(streamer):
    """
    True  = LIVE 확인
    False = OFF 확인
    None  = 확인 실패/타임아웃. 안전을 위해 병합하지 않음.
    """
    if not streamer:
        return None

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "streamlink", "--stream-url", streamer["url"], streamer.get("quality", "best")],
            capture_output=True,
            text=True,
            timeout=25,
            encoding="utf-8",
            errors="ignore",
        )
        if proc.returncode == 0 and bool(proc.stdout.strip()):
            return True
        return False
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None



# OFF_STREAMER_MERGE_BYPASS_PATCH_START
def streamer_is_merge_protected(streamer):
    """
    ON ?? ????? DB recording/LIVE ??? ?????.
    OFF/??? ????? ??? ?? ????.
    """
    if not streamer:
        return False

    for key in ("enabled", "is_enabled", "active", "recording_enabled"):
        if key in streamer:
            v = streamer.get(key)

            if isinstance(v, str):
                return v.strip().lower() not in ("0", "false", "no", "n", "off", "disable", "disabled")

            return bool(v)

    # enabled ??? ??? ?? ???? ?? ON ??
    return True
# OFF_STREAMER_MERGE_BYPASS_PATCH_END



# STREAMER_NAME_ON_CHECK_PATCH_START
def streamer_name_is_enabled_in_streamers_txt(streamer_name):
    """
    streamers.txt?? ?? ON? ???? ??? ?? ???? ???.
    OFF/??? ????? DB recording/LIVE ??? ???? ?? ?????.
    """
    try:
        from pathlib import Path

        base = Path(__file__).resolve().parent
        p = base / "streamers.txt"

        if not p.exists():
            return True

        def parse_bool(x):
            x = str(x).strip().lower()
            if x in ("1", "true", "yes", "y", "on", "enable", "enabled"):
                return True
            if x in ("0", "false", "no", "n", "off", "disable", "disabled"):
                return False
            return None

        enabled_names = set()

        for raw in p.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
            raw = raw.strip()

            if not raw or raw.startswith("#"):
                continue

            parts = [x.strip() for x in raw.split(",")]
            b = parse_bool(parts[0]) if parts else None

            if b is not None:
                if b and len(parts) >= 2:
                    enabled_names.add(parts[1])
            else:
                # ?? ??: ??,URL,quality ? ON?? ??
                if len(parts) >= 1:
                    enabled_names.add(parts[0])

        return streamer_name in enabled_names

    except Exception:
        # ?? ?? ? ???? ?? ??
        return True
# STREAMER_NAME_ON_CHECK_PATCH_END


def confirm_stream_offline(streamer, checks=3, interval_sec=30):
    """
    방제 변경/일시 끊김 때문에 LIVE 확인이 흔들릴 수 있으므로
    여러 번 연속 OFF일 때만 병합을 허용합니다.
    LIVE 또는 확인 실패가 한 번이라도 나오면 병합 대기합니다.
    """
    checks = max(1, int(checks))
    interval_sec = max(5, int(interval_sec))

    for i in range(checks):
        live = stream_is_live(streamer)
        if live is True:
            log(f"오프라인 확인 {i+1}/{checks}: 아직 LIVE", "📡")
            return False
        if live is None:
            log(f"오프라인 확인 {i+1}/{checks}: 확인 실패/타임아웃. 안전상 병합 대기", "⚠️")
            return False

        log(f"오프라인 확인 {i+1}/{checks}: OFF", "📴")
        if i < checks - 1:
            time.sleep(interval_sec)

    return True


def session_state(conn, session_id):
    """Return the recorder session state used by merge/upload safety gates."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, final_path, started_at, ended_at, error_message
                FROM recording_sessions
                WHERE session_id=%s
                LIMIT 1
                """,
                (session_id,),
            )
            row = cur.fetchone() or {}
            return {
                "status": str(row.get("status") or "").lower(),
                "final_path": str(row.get("final_path") or ""),
                "started_at": row.get("started_at"),
                "ended_at": row.get("ended_at"),
                "error_message": str(row.get("error_message") or ""),
            }
    except Exception:
        return {
            "status": "", "final_path": "", "started_at": None,
            "ended_at": None, "error_message": "",
        }


def mark_session_merged(conn, session_id, final_path, parts_count, note=None):
    """Publish the verified final as the authoritative session result.

    Historical sessions sometimes kept ``merge_failed`` or
    ``merge_blocked_missing_parts`` after a final had been successfully
    rebuilt.  The uploader then claimed the pending row and immediately
    demoted it to ``not_ready``.  Closing the session in the same merge path
    prevents that repeated queue loss.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recording_sessions
            SET status='merged',
                final_path=%s,
                parts_count=GREATEST(COALESCE(parts_count, 0), %s),
                error_message=%s
            WHERE session_id=%s
            """,
            (str(final_path), int(parts_count), note, str(session_id)),
        )
        return cur.rowcount


def session_still_recording(conn, session_id):
    """
    recorder_v07.py가 아직 같은 session_id를 실제 처리 중일 때만
    Drive worker가 먼저 최종 병합하지 못하게 막습니다.

    ffmpeg_failed/merge_failed/merge_blocked_missing_parts 같은 상태는
    작업이 진행 중이라는 뜻이 아니라 복구 병합이 필요하다는 뜻입니다.
    이를 active로 취급하면 복구기가 영원히 해당 세션에 진입하지 못합니다.
    """
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM recording_sessions WHERE session_id=%s LIMIT 1", (session_id,))
            row = cur.fetchone()
            status = str((row or {}).get("status", "")).lower()
            if status in {"recording", "processing", "merging"}:
                return True

            terminal_statuses = {
                "merged", "stitched", "stopped", "no_parts", "postprocess_timeout",
                "merge_failed", "merge_blocked_missing_parts", "exception",
            }
            if status not in terminal_statuses:
                cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM recording_parts
                    WHERE session_id=%s
                      AND status IN ('recording', 'processing')
                    """,
                    (session_id,),
                )
                row = cur.fetchone()
                if row and int(row.get("cnt", 0)) > 0:
                    return True
    except Exception:
        # 구버전 DB이거나 테이블 확인 실패면 여기서 막지 않고,
        # 아래 LIVE 확인 로직으로 한 번 더 안전장치를 둡니다.
        return False

    return False


def session_date_is_before_today(session_id):
    """
    session_id가 2026-06-21_... 처럼 날짜로 시작하면,
    오늘 이전 날짜는 DB 녹화중/LIVE 보호를 우회해도 되는 과거 세션으로 봅니다.
    오늘 날짜는 절대 우회하지 않습니다.
    """
    try:
        m = re.match(r"^(20\d{2}-\d{2}-\d{2})", str(session_id or ""))
        if not m:
            return False
        d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        return d < datetime.now().date()
    except Exception:
        return False


def _parse_dt(date_text, time_text):
    try:
        return datetime.strptime(f"{date_text} {time_text.replace('-', ':')}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def parse_session(parts_dir, streamers=None):
    name = parts_dir.name
    if not name.endswith("_parts"):
        return None

    base = name[:-6]
    broadcast_title = ""
    meta_streamer_name = ""
    meta_source_url = ""
    try:
        meta_path = parts_dir / "_session_meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            broadcast_title = str(meta.get("broadcast_title") or "").strip()
            meta_streamer_name = str(meta.get("streamer_name") or "").strip()
            meta_source_url = str(meta.get("source_url") or "").strip()
    except Exception:
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)_(\d{2}-\d{2}-\d{2})$", base)
    if not m:
        streamer = parts_dir.parent.parent.name if parts_dir.parent and parts_dir.parent.parent else "unknown"
        started_dt = datetime.fromtimestamp(parts_dir.stat().st_mtime)
        return {
            "session_id": base,
            "base": base,
            "streamer": streamer,
            "started_at": started_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "started_dt": started_dt,
            "format": "fallback",
            "broadcast_title": broadcast_title,
        }

    date_text, middle, time_text = m.group(1), m.group(2), m.group(3)
    started_dt = _parse_dt(date_text, time_text) or datetime.fromtimestamp(parts_dir.stat().st_mtime)

    streamer = None
    fmt = "unknown"

    # streamers.txt 기준으로 새 형식/구형 제목 포함 형식을 모두 안전하게 인식합니다.
    candidates = []
    if streamers:
        for original_name in streamers.keys():
            sn = sanitize(original_name, ) if False else sanitize(original_name)
            candidates.append((original_name, sn))
        candidates.sort(key=lambda x: len(x[1]), reverse=True)

    # 메타데이터에 녹화 당시 등록명이 있으면 파일명 추정보다 우선합니다.
    # 특히 "시라유키 히나"처럼 공백이 파일명에서 '_'로 바뀌는 이름을
    # 마지막 토큰("히나")으로 잘못 자르는 일을 막습니다.
    if meta_streamer_name:
        streamer = meta_streamer_name
        fmt = "metadata"
        if streamers:
            meta_norm = normalize_name(meta_streamer_name)
            for original_name, info in streamers.items():
                if normalize_name(original_name) == meta_norm or (
                    meta_source_url and str(info.get("url") or "") == meta_source_url
                ):
                    streamer = original_name
                    break

    for original_name, safe_name in candidates:
        if streamer is not None:
            break
        if middle == safe_name:
            streamer = original_name
            fmt = "new"
            break
        if middle.endswith("_" + safe_name):
            streamer = original_name
            fmt = "old_title"
            if not broadcast_title:
                broadcast_title = middle[:-(len(safe_name) + 1)].replace("_", " ").strip()
            break

    if streamer is None:
        # 등록 목록에 아직 반영되지 않은 새 스트리머도 이름 전체를 보존합니다.
        # 마지막 '_' 뒤만 취하면 복합 이름이 영구적으로 짧아질 수 있습니다.
        streamer = middle.replace("_", " ").strip()
        fmt = "new_fallback"

    return {
        "session_id": base,
        "base": base,
        "streamer": streamer,
        "started_at": started_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "started_dt": started_dt,
        "format": fmt,
        "broadcast_title": broadcast_title,
    }


def part_index(path):
    m = re.search(r"_part(\d{3,})\.(mp4|ts)$", path.name)
    if not m:
        return 0
    return int(m.group(1))


def parts_are_stable(parts, wait_sec=20):
    sizes1 = {}
    for p in parts:
        if not p.exists():
            return False
        sizes1[str(p)] = p.stat().st_size

    time.sleep(wait_sec)

    for p in parts:
        if not p.exists():
            return False
        if sizes1[str(p)] != p.stat().st_size:
            return False
    return True


def make_concat_list(parts, list_path):
    def q(p):
        # ffmpeg concat demuxer용 파일 목록입니다.
        # 로컬 staging 폴더의 단순 파일명만 사용하게 만들어 한글/이모지/따옴표 경로 문제를 줄입니다.
        return str(p).replace("\\", "/").replace("'", "\\'")
    list_path.write_text("\n".join([f"file '{q(p)}'" for p in parts]) + "\n", encoding="utf-8")


def _sum_sizes(paths):
    total = 0
    for p in paths:
        try:
            total += int(Path(p).stat().st_size)
        except Exception:
            pass
    return total


def _parts_signature(parts):
    items = []
    for path in parts:
        p = Path(path)
        try:
            stat = p.stat()
            items.append(f"{p.resolve()}|{stat.st_size}|{stat.st_mtime_ns}")
        except Exception:
            items.append(f"{p}|missing")
    return hashlib.sha256("\n".join(items).encode("utf-8", errors="replace")).hexdigest()


def _receipt_path(final_path):
    key = hashlib.sha256(str(Path(final_path).resolve()).encode("utf-8", errors="replace")).hexdigest()[:20]
    return RECEIPT_DIR / f"{key}.json"


def _write_merge_receipt(final_path, parts, salvage_report=None):
    final_path = Path(final_path)
    data = {
        "version": 2,
        "created_at": now(),
        "final_path": str(final_path.resolve()),
        "final_size": safe_file_size(final_path),
        "parts_signature": _parts_signature(parts),
        "parts_count": len(parts),
        "salvage": salvage_report or [],
    }
    receipt = _receipt_path(final_path)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    tmp = receipt.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(receipt)


def _verified_by_receipt(final_path, parts):
    try:
        data = json.loads(_receipt_path(final_path).read_text(encoding="utf-8"))
        return (
            str(Path(final_path).resolve()) == str(data.get("final_path"))
            and safe_file_size(final_path) == int(data.get("final_size") or 0)
            and _parts_signature(parts) == str(data.get("parts_signature") or "")
        )
    except Exception:
        return False


def _is_plausible_size(final_path, parts, min_ratio=0.85):
    try:
        fsize = int(Path(final_path).stat().st_size) if Path(final_path).exists() else 0
    except Exception:
        fsize = 0
    psize = _sum_sizes(parts)
    if fsize <= 0:
        return False, fsize, psize
    if psize <= 0:
        return True, fsize, psize
    return fsize >= int(psize * float(min_ratio)), fsize, psize


def _stage_root_for(final_path, cfg=None):
    """
    병합/복구용 staging은 config.json 값을 우선 사용합니다.

    우선순위:
    1) config.json merge_worker.staging_dir 값이 있으면 그 경로
    2) 설정이 없으면 final_path 부모의 _merge_staging
    """
    final_path = Path(final_path)
    safe = sanitize(final_path.stem)

    configured = None
    try:
        if cfg:
            configured = cfg.get("merge_worker", {}).get("staging_dir") or cfg.get("paths", {}).get("merge_staging_dir")
    except Exception:
        configured = None

    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(final_path.parent / "_merge_staging")

    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            test = root / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
        except Exception:
            continue

        stage = root / safe
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        return stage
    raise OSError(f"사용 가능한 merge staging 경로가 없습니다: {final_path}")


def _copy_parts_to_stage(parts, stage_dir):
    """Stage parts using NTFS hard links when possible.

    When recordings and staging are on the same volume, a hard link
    gives ffmpeg a simple ASCII path without consuming a second copy of tens or
    hundreds of gigabytes. Cross-volume or unsupported filesystems fall back to
    a verified copy.
    """
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged = []
    for i, src in enumerate(parts, 1):
        src = Path(src)
        ext = src.suffix.lower() or ".mp4"
        dst = stage_dir / f"part{i:04d}{ext}"
        try:
            os.link(str(src), str(dst))
            mode = "hardlink"
        except Exception:
            log(f"staging 복사 {i}/{len(parts)}: {src.name}", "📥")
            shutil.copy2(str(src), str(dst))
            mode = "copy"
        # 복사 직후 실제 크기가 같은지 확인합니다.
        try:
            if src.stat().st_size != dst.stat().st_size:
                log(f"staging 복사 크기 불일치: {src.name}", "❌")
                return []
        except Exception as e:
            log(f"staging 복사 확인 실패: {src.name} / {e}", "❌")
            return []
        staged.append(dst)
        if mode == "hardlink":
            log(f"staging 하드링크 {i}/{len(parts)}: {src.name} (추가 용량 0)", "🔗")
    return staged


def _run_ffmpeg_concat_demuxer(parts, out_path, list_path):
    make_concat_list(parts, list_path)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), "-c", "copy", str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0:
        log("concat demuxer 병합 실패", "❌")
        print(proc.stderr[-3000:], flush=True)
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def _ffmpeg_duration(path):
    """Read duration with ffmpeg itself; this install does not always include ffprobe."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)], capture_output=True, text=True,
            encoding="utf-8", errors="ignore", timeout=90,
        )
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
        if not match:
            return None
        return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    except Exception:
        return None


def _late_parts_extension_plan(final_path, parts, state):
    """Return a safe append plan for a final that stopped before late parts.

    A recorder crash can leave a playable prefix final plus one or more late
    parts.  Replacing the prefix with only those late parts loses most of the
    broadcast, while ignoring them leaves the session permanently blocked.
    We append only when the combined durations fit inside the recorded session
    window (with timestamp-overlap tolerance), so a part already present in a
    full final is not appended twice.
    """
    failed_terminal = {
        "merge_failed", "merge_blocked_missing_parts", "postprocess_timeout",
        "exception", "no_parts", "stopped",
    }
    if str(state.get("status") or "").lower() not in failed_terminal:
        return None
    final_duration = _ffmpeg_duration(final_path)
    part_durations = [_ffmpeg_duration(p) for p in parts]
    if not final_duration or not part_durations or any(not d or d < 1 for d in part_durations):
        return None

    started_at, ended_at = state.get("started_at"), state.get("ended_at")
    session_span = None
    try:
        if started_at and ended_at:
            session_span = max(0.0, (ended_at - started_at).total_seconds())
    except Exception:
        session_span = None

    added_duration = float(sum(part_durations))
    combined_duration = float(final_duration) + added_duration
    if added_duration < 10:
        return None
    if session_span:
        # A final already covering almost the whole session must not receive a
        # duplicate tail. Segment boundaries overlap slightly, so allow up to
        # ten minutes/5% around the DB wall-clock span.
        tolerance = max(600.0, session_span * 0.05)
        if final_duration >= session_span - 120:
            return None
        if combined_duration > session_span + tolerance:
            return None
    return {
        "parts": [Path(final_path)] + [Path(p) for p in parts],
        "final_duration": float(final_duration),
        "added_duration": added_duration,
        "combined_duration": combined_duration,
        "session_span": session_span,
    }


def _packet_scan_ok(path):
    """Fast full-file packet scan. No decode/re-encode and no output file is created."""
    if safe_file_size(path) <= 0:
        return False
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-err_detect", "ignore_err", "-i", str(path),
             "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "null", null_target],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        )
        return proc.returncode == 0
    except Exception:
        return False


def _salvage_part_to_ts(src, ts, index, total):
    """Preserve a damaged part using lossless remux first, then re-encode only that part."""
    src = Path(src)
    src_size = safe_file_size(src)
    src_duration = _ffmpeg_duration(src)
    log(f"TS salvage 1차 무손실 재래핑 {index}/{total}: {src.name}", "🧰")
    remux = subprocess.run([
        "ffmpeg", "-y", "-analyzeduration", "200M", "-probesize", "200M",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err", "-i", str(src),
        "-map", "0:v:0?", "-map", "0:a?", "-sn", "-dn", "-c", "copy",
        "-avoid_negative_ts", "make_zero", "-mpegts_flags", "+resend_headers+initial_discontinuity",
        "-f", "mpegts", str(ts),
    ], capture_output=True, text=True, encoding="utf-8", errors="ignore")

    ts_size = safe_file_size(ts)
    ts_duration = _ffmpeg_duration(ts)
    duration_ratio = (ts_duration / src_duration) if src_duration and ts_duration else None
    remux_good = ts_size > 0 and ((duration_ratio is not None and duration_ratio >= 0.97)
                                  or (duration_ratio is None and src_size > 0 and ts_size >= src_size * 0.60))
    if remux_good:
        if remux.returncode != 0:
            log(f"끝부분 오류를 버리고 무손실 복구: {src.name}", "⚠️")
        return True, "remux", src_duration, ts_duration

    # A broken packet/timestamp can stop stream-copy early. Decode around it and encode only
    # this damaged part. CRF 18 keeps visual loss very small while recovering later frames.
    ts.unlink(missing_ok=True)
    log(f"TS salvage 2차 손상 파트 선택 재인코딩: {src.name}", "🛠️")
    encoded = subprocess.run([
        "ffmpeg", "-y", "-analyzeduration", "200M", "-probesize", "200M",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err", "-i", str(src),
        "-map", "0:v:0?", "-map", "0:a?", "-sn", "-dn",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-af", "aresample=async=1:first_pts=0",
        "-avoid_negative_ts", "make_zero", "-mpegts_flags", "+resend_headers+initial_discontinuity",
        "-f", "mpegts", str(ts),
    ], capture_output=True, text=True, encoding="utf-8", errors="ignore")

    ts_size = safe_file_size(ts)
    ts_duration = _ffmpeg_duration(ts)
    duration_ratio = (ts_duration / src_duration) if src_duration and ts_duration else None
    encoded_good = ts_size > 0 and ((duration_ratio is not None and duration_ratio >= 0.90)
                                    or (src_duration is None and _packet_scan_ok(ts)))
    if encoded_good:
        log(f"손상 파트 선택 재인코딩 복구 완료: {src.name}"
            + (f" / 시간 보존 {duration_ratio*100:.1f}%" if duration_ratio is not None else ""), "✅")
        return True, "reencoded", src_duration, ts_duration

    log(f"복구 불가능한 파트 보존, 전체 병합 보류: {src.name}", "❌")
    if encoded.stderr:
        print(encoded.stderr[-2500:], flush=True)
    return False, "blocked", src_duration, ts_duration


def _run_ffmpeg_ts_fallback(parts, out_path, stage_dir, cfg=None):
    """Loss-minimizing salvage with an explicitly reported last-resort skip.

    Each part is first remuxed without quality loss, then only a damaged part is
    re-encoded. If both methods fail, the part may be skipped only when the
    config explicitly permits it. Every skip is written to salvage_report.json.
    """
    ts_dir = stage_dir / "ts_fallback"
    ts_dir.mkdir(parents=True, exist_ok=True)
    mw = (cfg or {}).get("merge_worker", {}) if isinstance(cfg, dict) else {}
    allow_skip = bool(mw.get("allow_skip_unrecoverable_parts", True))
    max_skips = max(0, int(mw.get("max_unrecoverable_part_skips", 2)))
    ts_parts, report = [], []
    skipped = 0
    for i, src in enumerate(parts, 1):
        ts = ts_dir / f"part{i:04d}.ts"
        ok, mode, src_duration, out_duration = _salvage_part_to_ts(src, ts, i, len(parts))
        report.append({"part": Path(src).name, "mode": mode,
                       "source_duration": src_duration, "salvaged_duration": out_duration})
        if not ok:
            skipped += 1
            report[-1]["mode"] = "skipped"
            if not allow_skip or skipped > max_skips or skipped >= len(parts):
                (stage_dir / "salvage_report.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"복구 불가 파트가 허용치를 초과해 병합 보류: {skipped}/{max_skips}", "❌")
                return False
            log(f"복구 불가 파트 제외 후 나머지 영상 보존: {Path(src).name}", "⚠️")
            continue
        ts_parts.append(ts)

    (stage_dir / "salvage_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    joined_ts = stage_dir / "joined.ts"
    log(f"TS salvage 바이너리 연결 시작: {len(ts_parts)}개", "🧩")
    try:
        with joined_ts.open("wb") as writer:
            for i, ts in enumerate(ts_parts, 1):
                log(f"TS salvage 연결 {i}/{len(ts_parts)}: {ts.name}", "🔗")
                with ts.open("rb") as reader:
                    shutil.copyfileobj(reader, writer, length=16 * 1024 * 1024)
    except Exception as exc:
        log(f"TS salvage 바이너리 연결 실패: {exc}", "❌")
        return False

    if safe_file_size(joined_ts) <= 0:
        return False
    log("TS salvage 최종 MP4 재래핑 시작", "🧩")
    proc = subprocess.run([
        "ffmpeg", "-y", "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-i", str(joined_ts), "-map", "0:v:0?", "-map", "0:a?", "-sn", "-dn",
        "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out_path),
    ], capture_output=True, text=True, encoding="utf-8", errors="ignore")

    if safe_file_size(out_path) <= 0 or not _packet_scan_ok(out_path):
        log("TS salvage 최종본 전체 패킷 검증 실패", "❌")
        if proc.stderr:
            print(proc.stderr[-3000:], flush=True)
        return False
    expected = sum(item["salvaged_duration"] or 0 for item in report)
    actual = _ffmpeg_duration(out_path)
    if expected > 0 and actual:
        log(f"TS salvage 재생시간 참고: 최종 {actual:.1f}s / 파트 합계 {expected:.1f}s", "ℹ️")
    log(
        f"TS salvage 검증 완료: 선택 재인코딩 "
        f"{sum(1 for x in report if x['mode']=='reencoded')}개 / 제외 {skipped}개",
        "✅",
    )
    return True

def _same_drive(a, b):
    try:
        return Path(a).drive.lower() == Path(b).drive.lower()
    except Exception:
        return False


def _publish_final_from_stage(tmp_final, final_path):
    """Publish a staged final file to its real destination safely.

    Windows cannot rename/replace files across different drives (WinError 17).
    If staging and the final file are on different volumes, copy to a temporary file
    beside the final file, verify size, then replace within the same drive.
    """
    tmp_final = Path(tmp_final)
    final_path = Path(final_path)

    if not tmp_final.exists() or tmp_final.stat().st_size <= 0:
        raise FileNotFoundError(f"staged final not found or empty: {tmp_final}")

    final_path.parent.mkdir(parents=True, exist_ok=True)

    if _same_drive(tmp_final, final_path):
        tmp_final.replace(final_path)
        return

    copy_tmp = final_path.with_name(f"{final_path.stem}.copying_{datetime.now():%Y%m%d_%H%M%S}{final_path.suffix}")
    log(f"다른 드라이브 최종본 게시: 복사 후 교체 방식 사용 ({tmp_final.drive} → {final_path.drive})", "🚚")
    try:
        shutil.copy2(str(tmp_final), str(copy_tmp))
        src_size = tmp_final.stat().st_size
        dst_size = copy_tmp.stat().st_size
        if src_size != dst_size:
            raise IOError(f"copy size mismatch: src {src_size} / dst {dst_size}")
        copy_tmp.replace(final_path)
        try:
            tmp_final.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception:
        try:
            copy_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise





def _ffmpeg_concat_unlocked(parts, final_path, list_path, cfg=None):
    final_path = Path(final_path)
    stage_root = _stage_root_for(final_path, cfg)
    stage_parts_dir = stage_root / "parts"
    tmp_final = stage_root / f"{final_path.stem}.tmp.mp4"
    stage_list = stage_root / "concat.txt"

    log(f"최종 병합 준비: {len(parts)}개 → {final_path.name}", "🧩")
    log(f"D드라이브 staging 사용: {stage_root}", "🛡️")

    success = False
    try:
        staged_parts = _copy_parts_to_stage(parts, stage_parts_dir)
        if len(staged_parts) != len(parts):
            log("Drive staging 복사 실패로 병합 중단", "❌")
            return False

        log(f"Drive staging 병합 시작: {len(staged_parts)}개", "🧩")
        ok = _run_ffmpeg_concat_demuxer(staged_parts, tmp_final, stage_list)
        ok_size, fsize, psize = _is_plausible_size(tmp_final, staged_parts)
        if ok and ok_size and not _packet_scan_ok(tmp_final):
            log("1차 병합본 전체 패킷 검사 실패. 복구 경로로 전환합니다.", "🚨")
            ok = False

        if not ok or not ok_size:
            log(f"1차 병합 결과가 너무 작거나 실패했습니다: final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "🚨")
            try:
                tmp_final.unlink(missing_ok=True)
            except Exception:
                pass
            log("TS fallback 무손실 재래핑 병합을 시도합니다.", "🧰")
            ok = _run_ffmpeg_ts_fallback(staged_parts, tmp_final, stage_root, cfg)
            # 선택 재인코딩된 손상 파트는 원본보다 파일 크기가 작아질 수 있으므로
            # fallback 자체의 전체 패킷/재생시간 검증 결과를 신뢰합니다.
            fsize, psize = safe_file_size(tmp_final), _sum_sizes(staged_parts)
            if not ok:
                log(f"TS fallback 최종 검증 실패: final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "❌")
                return False

        salvage_report = []
        report_path = stage_root / "salvage_report.json"
        if report_path.exists():
            try:
                salvage_report = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                salvage_report = []

        # The staged result has already passed a full packet scan above (the
        # TS fallback performs the same scan internally).  On the same volume
        # ``replace`` is only an atomic rename, so scanning the identical
        # 30~100GB file a second time wastes several minutes and disk I/O.
        # Cross-volume publishing still copies bytes and therefore keeps the
        # post-copy packet scan.
        same_volume_publish = _same_drive(tmp_final, final_path)
        _publish_final_from_stage(tmp_final, final_path)
        fsize, psize = safe_file_size(final_path), _sum_sizes(staged_parts)
        if fsize <= 0 or (not same_volume_publish and not _packet_scan_ok(final_path)):
            log(f"최종본 게시 후 전체 패킷 검증 실패: final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "❌")
            return False
        _write_merge_receipt(final_path, parts, salvage_report)
        success = True
        log(f"최종 병합 완료: {final_path} / {final_path.stat().st_size/1024**3:.2f}GB", "✅")
        return True

    except Exception as e:
        log(f"최종 병합 중 오류: {e}", "❌")
        return False
    finally:
        try:
            if success:
                shutil.rmtree(stage_root, ignore_errors=True)
            else:
                # 실패 시에는 원인 확인/수동 복구를 위해 staging을 남깁니다.
                # 다음 성공 작업 또는 사용자가 확인 후 지워도 됩니다.
                log(f"병합 실패/중단으로 staging 보존: {stage_root}", "🧯")
        except Exception:
            pass


def ffmpeg_concat(parts, final_path, list_path, cfg=None):
    """Public unified merge entry point used by recorder and background worker."""
    handle = _acquire_merge_operation()
    if not handle:
        log("다른 병합 작업 대기 시간이 초과되었습니다. 다음 차례에 재시도합니다.", "⏳")
        return False
    try:
        return _ffmpeg_concat_unlocked(parts, final_path, list_path, cfg)
    finally:
        _release_merge_operation(handle)

def safe_file_size(path):
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return int(p.stat().st_size)
    except Exception:
        pass
    return 0


def total_size(paths):
    total = 0
    for p in paths:
        total += safe_file_size(p)
    return total



def final_size_is_plausible(final_path, parts, min_ratio=0.85):
    """
    concat -c copy 결과는 보통 입력 part 총합과 거의 비슷해야 합니다.
    기존 final 파일이 너무 작으면 예전 실패/부분 병합본으로 보고 재병합합니다.
    """
    fsize = safe_file_size(final_path)
    psize = total_size(parts)
    if fsize <= 0:
        return False, fsize, psize
    if psize <= 0:
        return True, fsize, psize
    # 선택 재인코딩/복구 불가 파트 제외로 최종 파일 크기가 작아질 수 있습니다.
    # 병합 직후 전체 패킷 검사를 통과하고 원본 서명이 일치한 영수증이 있으면
    # 단순 용량 비율만으로 정상 복구본을 폐기하지 않습니다.
    if _verified_by_receipt(final_path, parts):
        return True, fsize, psize
    # 일부 컨테이너 오버헤드 차이는 허용하지만, 27GB -> 3GB 같은 경우는 무조건 실패로 봅니다.
    return fsize >= int(psize * float(min_ratio)), fsize, psize


def cleanup_completed_parts_dir(parts_dir, final_path, cfg=None, included_parts=None):
    """Remove duplicate source parts after a verified final has been published.

    This is intentionally opt-in and uses a stricter size floor than the merge
    acceptance check.  A failed/short merge therefore keeps its source parts
    available for recovery instead of silently losing footage.
    """
    mw = (cfg or {}).get("merge_worker", {}) if isinstance(cfg, dict) else {}
    if not bool(mw.get("delete_parts_after_merge", False)):
        return False
    parts_dir = Path(parts_dir)
    final_path = Path(final_path)
    if not parts_dir.is_dir() or not final_path.is_file() or final_path.stat().st_size <= 0:
        return False
    # Only remove the exact snapshot that was consumed by this merge.  A
    # sweeper can add a late part while ffmpeg is running; deleting the whole
    # directory in that case silently loses the late footage.
    if included_parts is None:
        log(f"원본 part 보존(소비 스냅샷 없음): {parts_dir.name}", "🛡️")
        return False
    files = [Path(p) for p in included_parts if Path(p).is_file()]
    files = [p for p in files if p.parent == parts_dir or parts_dir in p.parents]
    part_size = sum(int(p.stat().st_size) for p in files if p.suffix.lower() in {".mp4", ".ts"})
    final_size = int(final_path.stat().st_size)
    min_ratio = float(mw.get("parts_cleanup_min_ratio", 0.95))
    ratio = (final_size / part_size) if part_size else 1.0
    if part_size and ratio < min_ratio:
        log(f"원본 part 보존(최종본 크기 부족): {parts_dir.name} / final {ratio:.3f} < {min_ratio:.3f}", "🛡️")
        return False
    try:
        deleted = 0
        for p in files:
            try:
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
        # Remove only empty directories.  Any file that appeared after the
        # snapshot remains available for a later merge/recovery pass.
        for d in sorted([p for p in parts_dir.rglob("*") if p.is_dir()] + [parts_dir], key=lambda x: len(x.parts), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass
        remaining = [p for p in parts_dir.rglob("*") if p.is_file()] if parts_dir.exists() else []
        if remaining:
            log(f"소비한 part만 삭제, 늦게 도착한 part 보존: {parts_dir.name} / 삭제 {deleted}개 / 잔여 {len(remaining)}개", "🛡️")
        else:
            log(f"병합 완료 원본 part 정리: {parts_dir} / {part_size / 1024**3:.2f}GB", "🧹")
        return deleted > 0
    except Exception as exc:
        log(f"병합 완료 원본 part 폴더 삭제 실패: {parts_dir} / {exc}", "⚠️")
        return False


def backup_bad_final(final_path):
    final_path = Path(final_path)
    if not final_path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bad = final_path.with_name(f"{final_path.stem}.bad_small_{stamp}{final_path.suffix}")
    try:
        final_path.replace(bad)
        log(f"기존 최종본이 part 총합보다 너무 작아 백업 후 재병합합니다: {bad.name}", "⚠️")
        return bad
    except Exception as e:
        log(f"작은 최종본 백업 실패: {final_path} / {e}", "❌")
        return None


def update_recording_row_for_session(
    conn, session_id, final_path, parts_dir, parts_count,
    youtube_status="pending", accepted_marker=None,
):
    size_mb = 0
    try:
        size_mb = round(Path(final_path).stat().st_size / (1024 * 1024), 2)
    except Exception:
        pass
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recordings
            SET final_path=%s,
                file_size_mb=%s,
                parts_dir=%s,
                parts_count=%s,
                status='done',
                youtube_status=CASE
                    WHEN youtube_status IN (
                        'uploading','processing','uploaded_pending_youtube_processing','done'
                    ) THEN youtube_status
                    ELSE %s
                END,
                youtube_error=CASE
                    WHEN %s IS NULL OR %s='' THEN youtube_error
                    ELSE LEFT(CONCAT(
                        %s,
                        CASE
                            WHEN youtube_error IS NULL OR youtube_error='' THEN ''
                            ELSE CONCAT(' | previous=', youtube_error)
                        END
                    ), 1000)
                END,
                error_message=LEFT(CONCAT(IFNULL(error_message,''),
                    CASE WHEN IFNULL(error_message,'')='' THEN '' ELSE ' | ' END,
                    'size-guard repaired/revalidated final'), 1000)
            WHERE session_id=%s
              AND youtube_status NOT IN ('done','uploaded','success')
            """,
            (
                str(final_path), size_mb, str(parts_dir), int(parts_count), youtube_status,
                accepted_marker, accepted_marker, accepted_marker, str(session_id),
            ),
        )
        return cur.rowcount


def find_parts_dirs(drive_root):
    return sorted([p for p in drive_root.rglob("*_parts") if p.is_dir()])


def build_entries(parts_dirs, streamers):
    entries = []
    for parts_dir in parts_dirs:
        info = parse_session(parts_dir, streamers)
        if not info:
            continue
        final_path = parts_dir.parent / f"{info['base']}.mp4"
        candidates = list(parts_dir.glob("*_part*.mp4")) + list(parts_dir.glob("*_part*.ts"))
        parts = sorted(
            {p.resolve(): p for p in candidates if p.is_file() and p.stat().st_size > 0}.values(),
            key=lambda p: (part_index(p), p.stat().st_mtime, p.name),
        )

        if not parts:
            continue

        newest_mtime = max(p.stat().st_mtime for p in parts)
        oldest_mtime = min(p.stat().st_mtime for p in parts)
        entries.append({
            "parts_dir": parts_dir,
            "info": info,
            "streamer": info["streamer"],
            "session_id": info["session_id"],
            "base": info["base"],
            "started_dt": info.get("started_dt") or datetime.fromtimestamp(oldest_mtime),
            "started_at": info["started_at"],
            "parts": parts,
            "newest_mtime": newest_mtime,
            "oldest_mtime": oldest_mtime,
            "final_path": final_path,
        })
    return entries


def group_entries_for_stitch(entries, gap_sec):
    """
    같은 스트리머의 여러 _parts 폴더를 시간순으로 묶습니다.
    gap_sec보다 오래 끊긴 경우는 다른 방송일 수 있으므로 분리합니다.
    """
    by_streamer = {}
    for e in entries:
        by_streamer.setdefault(normalize_name(e["streamer"]), []).append(e)

    groups = []
    for _, items in by_streamer.items():
        items.sort(key=lambda e: (e["started_dt"], e["oldest_mtime"]))
        cur = []
        cur_end = None
        for e in items:
            st_ts = e["started_dt"].timestamp() if e.get("started_dt") else e["oldest_mtime"]
            if not cur:
                cur = [e]
                cur_end = max(e["newest_mtime"], st_ts)
                continue

            gap = st_ts - float(cur_end or st_ts)
            if gap <= gap_sec:
                cur.append(e)
                cur_end = max(float(cur_end or 0), e["newest_mtime"], st_ts)
            else:
                groups.append(cur)
                cur = [e]
                cur_end = max(e["newest_mtime"], st_ts)
        if cur:
            groups.append(cur)
    return groups


def group_is_idle(group, idle_after_last_part_sec):
    newest = max(e["newest_mtime"] for e in group)
    return (time.time() - newest) >= idle_after_last_part_sec


def group_parts(group):
    ordered = []
    for e in sorted(group, key=lambda x: (x["started_dt"], x["oldest_mtime"])):
        ordered.extend(sorted(e["parts"], key=lambda p: (part_index(p), p.stat().st_mtime)))
    return ordered


def stitched_base_for_group(group):
    first = sorted(group, key=lambda x: (x["started_dt"], x["oldest_mtime"]))[0]
    streamer = first["streamer"]
    dt = first["started_dt"]
    return f"{dt:%Y-%m-%d}_{sanitize(streamer)}_{dt:%H-%M-%S}_stitched"


def move_parts_to_stitched_dir(parts, stitched_parts_dir, stitched_base):
    """
    업로드 성공 후 parts 폴더 정리가 가능하도록, 여러 세션의 part 파일을
    하나의 stitched parts 폴더로 모읍니다. 최종본 생성 성공 후에만 실행됩니다.
    """
    stitched_parts_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for i, src in enumerate(parts, 1):
        dst = stitched_parts_dir / f"{stitched_base}_part{i:03d}.mp4"
        if dst.exists() and dst.stat().st_size > 0:
            moved.append(dst)
            continue
        try:
            shutil.move(str(src), str(dst))
            moved.append(dst)
        except Exception as e:
            log(f"stitched parts 폴더 이동 실패: {src} → {dst} / {e}", "⚠️")
    return moved


def cleanup_empty_source_dirs(group, stitched_parts_dir):
    for e in group:
        p = e["parts_dir"]
        if p == stitched_parts_dir:
            continue
        try:
            if p.exists() and not any(p.iterdir()):
                p.rmdir()
                log(f"빈 source parts 폴더 정리: {p.name}", "🧹")
        except Exception:
            pass


def process_stitch_group(group, cfg, logs_dir, streamers, require_offline, offline_checks, offline_interval, move_parts=True):
    if len(group) < 2:
        return False

    streamer_name = group[0]["streamer"]
    streamer = find_streamer_info(streamers, streamer_name)

    if not group_is_idle(group, int(cfg.setdefault("merge_worker", {}).get("idle_after_last_part_sec", 600))):
        return False

    group_is_past_date = all(session_date_is_before_today(e["session_id"]) for e in group)
    try:
        with db_connect(cfg) as conn:
            for e in group:
                # 전날 이전 세션은 DB의 recording 표기가 비정상 종료 뒤
                # 남은 흔적일 수 있습니다. 아래 OFF 확인과 파일 안정성
                # 검사를 모두 통과해야 하므로 복구 진입만 허용합니다.
                if session_still_recording(conn, e["session_id"]) and not group_is_past_date:
                    log_throttled(
                        f"stitch-db-wait:{e['session_id']}",
                        f"DB상 아직 후처리/실패 파트가 남아 자동 묶음 병합 대기: {streamer_name} / {e['session_id']}",
                        "🛡️",
                    )
                    return False
                state = session_state(conn, e["session_id"])
                if state.get("status") == "stitched" and state.get("final_path"):
                    log_throttled(
                        f"stitched-source:{e['session_id']}",
                        f"이미 stitched 처리된 source는 자동 묶음 대상에서 제외: {e['session_id']}",
                        "🛡️",
                    )
                    return False
    except Exception as e:
        log(f"DB 세션 상태 확인 실패. 안전을 위해 자동 묶음 병합 대기: {e}", "⚠️")
        return False

    if require_offline and streamer_name_is_enabled_in_streamers_txt(streamer_name):
        if not streamer:
            log(f"streamers.txt에서 스트리머 정보를 못 찾아 자동 묶음 병합 대기: {streamer_name}", "🛡️")
            return False
        if not confirm_stream_offline(streamer, offline_checks, offline_interval):
            log(f"방송 종료가 확실하지 않아 자동 묶음 병합 대기: {streamer_name} / sessions {len(group)}개", "📡")
            return False

    parts = group_parts(group)
    if not parts:
        return False

    if not parts_are_stable(parts, wait_sec=20):
        log(f"자동 묶음 병합 대상 part 크기 변동 감지. 다음 루프에서 재확인: {streamer_name}", "🧊")
        return False

    stitched_base = stitched_base_for_group(group)
    final_dir = group[0]["parts_dir"].parent
    final_path = final_dir / f"{stitched_base}.mp4"
    stitched_parts_dir = final_dir / f"{stitched_base}_parts"
    stitched_session_id = stitched_base

    if final_path.exists() and final_path.stat().st_size > 0:
        ok_size, fsize, psize = final_size_is_plausible(final_path, parts)
        if _verified_by_receipt(final_path, parts) and _packet_scan_ok(final_path):
            log(f"자동 묶음 최종본 이미 있음: {final_path.name} / final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "🧩")
        else:
            log_throttled(
                f"unverified-stitched-final:{final_path.resolve()}",
                f"기존 stitched 최종본과 현재 part의 동일성 미확인. 자동 덮어쓰기/삭제 보류: {final_path.name}",
                "🛡️",
            )
            return False
    else:
        concat_list = logs_dir / f"{stitched_base}_concat.txt"
        log(f"자동 묶음 병합 대상: {streamer_name} / 세션 {len(group)}개 / part {len(parts)}개", "🧷")
        for e in group:
            log(f"  - {e['parts_dir'].name} / part {len(e['parts'])}개", "   ")
        if not ffmpeg_concat(parts, final_path, concat_list, cfg):
            return False
        ok_size, fsize, psize = final_size_is_plausible(final_path, parts)
        if not ok_size:
            log(f"병합 후 최종본이 part 총합보다 너무 작습니다: final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "❌")
            backup_bad_final(final_path)
            return False

    if move_parts:
        moved = move_parts_to_stitched_dir(parts, stitched_parts_dir, stitched_base)
        if moved:
            cleanup_empty_source_dirs(group, stitched_parts_dir)
            parts_count = len(moved)
        else:
            parts_count = len(parts)
    else:
        parts_count = len(parts)

    try:
        with db_connect(cfg) as conn:
            source_ids = [e["session_id"] for e in group]
            final_parts_dir_for_db = stitched_parts_dir if move_parts else group[0]["parts_dir"]
            if recording_exists(conn, stitched_session_id):
                log(f"자동 묶음 최종본이 이미 DB에 등록되어 있음. DB 경로/크기 갱신: {stitched_session_id}", "🗃️")
                update_recording_row_for_session(conn, stitched_session_id, final_path, final_parts_dir_for_db, parts_count, youtube_status="pending")
                mark_source_recordings_not_requested(conn, source_ids, stitched_session_id)
            else:
                size_mb = final_path.stat().st_size / (1024 * 1024)
                rec_id = insert_recording(conn, {
                    "streamer_name": streamer_name,
                    "broadcast_title": next((e["info"].get("broadcast_title") for e in group if e["info"].get("broadcast_title")), ""),
                    "source_url": streamer["url"] if streamer else "",
                    "quality": streamer.get("quality", "best") if streamer else "best",
                    "started_at": group[0]["started_at"],
                    "final_path": str(final_path),
                    "file_size_mb": round(size_mb, 2),
                    "parts_dir": str(final_parts_dir_for_db),
                    "parts_count": parts_count,
                    "session_id": stitched_session_id,
                    "error_message": "auto-stitched from: " + ", ".join(source_ids)[:800],
                })
                mark_source_recordings_not_requested(conn, source_ids, stitched_session_id)
                mark_source_sessions_stitched(conn, source_ids, final_path)
                log(f"자동 묶음 YouTube 업로드 대기열 등록 완료 #{rec_id}: {final_path.name}", "📤")
        # 최종본/DB 등록이 끝난 뒤에만 중복 원본 part를 정리한다.
        # 크기 차이가 큰 복구본은 cleanup_completed_parts_dir가 보존한다.
        if move_parts:
            cleanup_completed_parts_dir(stitched_parts_dir, final_path, cfg, included_parts=moved)
        else:
            for source in group:
                cleanup_completed_parts_dir(source["parts_dir"], final_path, cfg, included_parts=source["parts"])
        return True
    except Exception as e:
        log(f"자동 묶음 DB 등록 실패: {e}", "❌")
        return False


def process_single_dir(entry, cfg, logs_dir, streamers, require_offline, offline_checks, offline_interval, idle_after_last_part_sec):
    info = entry["info"]
    parts_dir = entry["parts_dir"]
    final_path = entry["final_path"]

    if (time.time() - entry["newest_mtime"]) < idle_after_last_part_sec:
        return False

    streamer_name = info["streamer"]
    streamer = find_streamer_info(streamers, streamer_name)
    if not streamer_name_is_enabled_in_streamers_txt(streamer_name):
        log(f"???? OFF/?? ??? DB/LIVE ?? ??: {streamer_name}", "??")

    session_is_past_date = session_date_is_before_today(info["session_id"])
    try:
        with db_connect(cfg) as conn:
            state = session_state(conn, info["session_id"])
            # A source session already folded into another stitched session is
            # authoritative elsewhere.  Never recreate/clean it as a new
            # one-hour final when a late part directory appears.
            db_final = state.get("final_path") or ""
            if state.get("status") == "stitched" and db_final:
                try:
                    if Path(db_final).resolve() != final_path.resolve():
                        log_throttled(
                            f"stitched-source-hold:{info['session_id']}:{db_final}",
                            f"이미 stitched 최종본에 포함된 source 보류: {info['session_id']} -> {db_final}",
                            "🛡️",
                        )
                        return False
                except OSError:
                    log_throttled(
                        f"stitched-source-hold:{info['session_id']}",
                        f"이미 stitched 최종본에 포함된 source 보류: {info['session_id']}",
                        "🛡️",
                    )
                    return False
            if session_still_recording(conn, info["session_id"]) and not session_is_past_date:
                log_throttled(
                    f"single-db-wait:{info['session_id']}",
                    f"DB상 아직 후처리/실패 파트가 남아 병합 대기: {streamer_name} / {info['session_id']}",
                    "🛡️",
                )
                return False
    except Exception as e:
        log(f"DB 세션 상태 확인 실패. 안전을 위해 병합 대기: {e}", "⚠️")
        return False

    existing_final_ok = False
    accepted_marker = None
    if final_path.exists() and final_path.stat().st_size > 0:
        # A receipt is the only proof that this exact part snapshot produced
        # the final.  Size similarity alone is unsafe for one-hour late parts.
        if _verified_by_receipt(final_path, entry["parts"]) and _packet_scan_ok(final_path):
            existing_final_ok = True
            log(f"동일 part 영수증이 확인된 단일 세션 최종본 재사용: {final_path.name}", "✅")
        elif state.get("status") == "merged" and state.get("final_path"):
            try:
                existing_final_ok = Path(state["final_path"]).resolve() == final_path.resolve() and _packet_scan_ok(final_path)
            except OSError:
                existing_final_ok = False
            if existing_final_ok:
                log(f"DB merged 최종본 재사용(늦은 part는 별도 보존): {final_path.name}", "✅")
                # Do not delete newly arrived parts that are not proven to be
                # in this already-finished final.
                return False
        else:
            extension = _late_parts_extension_plan(final_path, entry["parts"], state)
            if extension:
                old_duration = extension["final_duration"]
                added_duration = extension["added_duration"]
                backup = final_path.with_name(
                    f"{final_path.stem}.pre_late_repair_{datetime.now():%Y%m%d_%H%M%S}{final_path.suffix}"
                )
                try:
                    try:
                        os.link(str(final_path), str(backup))
                    except OSError:
                        shutil.copy2(str(final_path), str(backup))
                    concat_list = logs_dir / f"{info['base']}_late_parts_concat.txt"
                    log(
                        f"기존 최종본 뒤 늦은 part {len(entry['parts'])}개 자동 복구: "
                        f"{final_path.name}",
                        "🧩",
                    )
                    repaired = ffmpeg_concat(extension["parts"], final_path, concat_list, cfg)
                    repaired_duration = _ffmpeg_duration(final_path) if repaired else None
                    min_growth = max(10.0, added_duration * 0.75)
                    if (
                        repaired and repaired_duration
                        and repaired_duration >= old_duration + min_growth
                        and _packet_scan_ok(final_path)
                    ):
                        existing_final_ok = True
                        accepted_marker = (
                            "validated best-available final covers recovery sources; "
                            f"late_parts={len(entry['parts'])}; "
                            f"duration={repaired_duration:.1f}s"
                        )
                        backup.unlink(missing_ok=True)
                        log(
                            f"늦은 part 복구 검증 완료: {old_duration:.1f}s → "
                            f"{repaired_duration:.1f}s",
                            "✅",
                        )
                    else:
                        if backup.exists():
                            backup.replace(final_path)
                        log("늦은 part 복구 검증 실패. 기존 최종본을 복원하고 보류합니다.", "❌")
                except Exception as exc:
                    try:
                        if backup.exists():
                            backup.replace(final_path)
                    except Exception:
                        pass
                    log(f"늦은 part 자동 복구 오류: {exc}", "❌")
        if not existing_final_ok:
            log_throttled(
                f"unverified-final:{final_path.resolve()}",
                f"기존 최종본과 현재 part의 동일성 미확인. 자동 덮어쓰기/삭제 보류: {final_path.name}",
                "🛡️",
            )
            return False

    if require_offline and streamer_name_is_enabled_in_streamers_txt(streamer_name):
        if not streamer:
            log(f"streamers.txt에서 스트리머 정보를 못 찾아 안전상 병합 대기: {streamer_name} / {parts_dir.name}", "🛡️")
            return False
        if not confirm_stream_offline(streamer, offline_checks, offline_interval):
            log(f"방송 종료가 확실하지 않아 병합 대기: {streamer_name} / parts {len(entry['parts'])}개", "📡")
            return False

    if not existing_final_ok and not parts_are_stable(entry["parts"], wait_sec=20):
        log(f"part 파일 크기 변동 감지. 다음 루프에서 재확인: {parts_dir.name}", "🧊")
        return False

    if not existing_final_ok:
        concat_list = logs_dir / f"{info['base']}_concat.txt"
        if not ffmpeg_concat(entry["parts"], final_path, concat_list, cfg):
            return False
        ok_size, fsize, psize = final_size_is_plausible(final_path, entry["parts"])
        if not ok_size:
            log(f"병합 후 최종본 검증 영수증이 없습니다: final {fsize/1024**3:.2f}GB / parts {psize/1024**3:.2f}GB", "❌")
            backup_bad_final(final_path)
            return False

    with db_connect(cfg) as conn:
        mark_session_merged(
            conn,
            info["session_id"],
            final_path,
            len(entry["parts"]),
            note=(accepted_marker or None),
        )
        if recording_exists(conn, info["session_id"]):
            log(f"이미 DB에 등록된 세션이라 DB 경로/크기만 갱신: {info['session_id']}", "🗃️")
            update_recording_row_for_session(
                conn,
                info["session_id"],
                final_path,
                parts_dir,
                len(entry["parts"]),
                youtube_status="pending",
                accepted_marker=accepted_marker,
            )
        else:
            size_mb = final_path.stat().st_size / (1024 * 1024)
            rec_id = insert_recording(conn, {
                "streamer_name": streamer_name,
                "broadcast_title": info.get("broadcast_title", ""),
                "source_url": streamer["url"] if streamer else "",
                "quality": streamer.get("quality", "best") if streamer else "best",
                "started_at": info["started_at"],
                "final_path": str(final_path),
                "file_size_mb": round(size_mb, 2),
                "parts_dir": str(parts_dir),
                "parts_count": len(entry["parts"]),
                "session_id": info["session_id"],
                "error_message": None,
            })
            log(f"YouTube 업로드 대기열 등록 완료 #{rec_id}: {final_path.name}", "📤")
    # 업로드 전에 병합 원본 part를 정리하되, 최종본 크기가 크게 줄어든
    # 복구 결과는 안전상 남겨 둔다.
    cleanup_completed_parts_dir(parts_dir, final_path, cfg, included_parts=entry["parts"])
    return True


def main():
    if not acquire_single_instance():
        print("[SINGLE_INSTANCE] Unified Merge Worker가 이미 실행 중입니다. 새 실행을 종료합니다.", flush=True)
        raise SystemExit(73)

    cfg = load_config()
    drive_root = Path(cfg["paths"]["drive_dir"])
    logs_dir = Path(cfg["paths"]["logs_dir"])
    logs_dir.mkdir(parents=True, exist_ok=True)

    mw = cfg.setdefault("merge_worker", {})
    interval_sec = int(mw.get("scan_interval_sec", 120))
    idle_after_last_part_sec = int(mw.get("idle_after_last_part_sec", 600))
    require_offline = bool(mw.get("require_stream_offline", True))
    offline_confirm_checks = int(mw.get("offline_confirm_checks", 3))
    offline_confirm_interval_sec = int(mw.get("offline_confirm_interval_sec", 30))
    auto_stitch_enabled = bool(mw.get("auto_stitch_enabled", True))
    auto_stitch_gap_sec = int(mw.get("auto_stitch_gap_sec", 3600))
    auto_stitch_move_parts = bool(mw.get("auto_stitch_move_parts", False))
    enabled = bool(mw.get("enabled", True))

    print("=" * 72)
    log(APP_NAME, "🚀")
    log(f"Drive root: {drive_root}", "☁️")
    log(f"스캔 주기: {interval_sec}초", "⏱️")
    log(f"마지막 part 후 대기: {idle_after_last_part_sec}초", "🕒")
    log(f"방송 종료 확인 필요: {require_offline}", "📡")
    log(f"OFF 연속 확인: {offline_confirm_checks}회 / {offline_confirm_interval_sec}초 간격", "🛡️")
    log(f"자동 묶음 병합: {auto_stitch_enabled} / gap {auto_stitch_gap_sec}초", "🧷")
    print("=" * 72)

    if not enabled:
        log("merge_worker.enabled=false 상태입니다.", "⛔")
        return

    while True:
        try:
            # 실행 중 streamers.txt가 바뀌어도 새 등록명을 즉시 반영합니다.
            streamers = read_streamers()
            parts_dirs = find_parts_dirs(drive_root)
            entries = build_entries(parts_dirs, streamers)

            handled_session_ids = set()

            if auto_stitch_enabled:
                groups = group_entries_for_stitch(entries, auto_stitch_gap_sec)
                for group in groups:
                    if len(group) < 2:
                        continue
                    # 묶음 대상은 성공/보류/실패 여부와 관계없이 이 루프에서
                    # 단일 세션 병합으로 다시 내려보내지 않습니다. 실패한 묶음을
                    # 같은 주기에 여러 방식으로 중복 처리하지 않게 합니다.
                    group_ids = {e["session_id"] for e in group}
                    if process_stitch_group(
                        group, cfg, logs_dir, streamers, require_offline,
                        offline_confirm_checks, offline_confirm_interval_sec,
                        move_parts=auto_stitch_move_parts,
                    ):
                        handled_session_ids.update(group_ids)
                    else:
                        handled_session_ids.update(group_ids)

            # 자동 묶음 대상이 아닌 단일 세션은 기존 방식대로 처리합니다.
            for entry in entries:
                if entry["session_id"] in handled_session_ids:
                    continue
                process_single_dir(
                    entry, cfg, logs_dir, streamers, require_offline,
                    offline_confirm_checks, offline_confirm_interval_sec,
                    idle_after_last_part_sec,
                )

            time.sleep(interval_sec)

        except KeyboardInterrupt:
            log("Merge Worker 종료", "🛑")
            break
        except Exception as e:
            log(f"Merge Worker 오류: {e}", "❌")
            time.sleep(interval_sec)


if __name__ == "__main__":
    main()

