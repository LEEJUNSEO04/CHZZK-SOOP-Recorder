
# Process-scoped Windows mutex: no stale file and no duplicate recorder process.
import ctypes as _si_ctypes
import sys as _si_sys

_si_mutex = _si_ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\CHZZK_RECORDER_ENGINE_V1")
if not _si_mutex:
    raise OSError("Recorder mutex creation failed")
if _si_ctypes.windll.kernel32.GetLastError() == 183:
    print("[SINGLE_INSTANCE] recorder가 이미 실행 중입니다. 중복 실행을 종료합니다.", flush=True)
    _si_sys.exit(73)


import concurrent.futures
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import pymysql
import requests

from title_filters import required_title_keywords, title_is_allowed, title_is_definitive

APP_NAME = "CHZZK · SOOP Recorder"



# CHZZK_V083_LOCK_FIX_START

# PLATFORM_DETECT_PATCH_START
def detect_platform_from_url(url: str) -> str:
    u = str(url or "").lower()

    if "chzzk.naver.com" in u:
        return "chzzk"

    if "sooplive.com" in u or "play.sooplive.com" in u:
        return "soop"

    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"

    return "unknown"
# PLATFORM_DETECT_PATCH_END


def normalize_stream_url(url: str) -> str:
    """Keep SOOP URLs pinned to a channel, never to an expired broadcast number."""
    text = str(url or "").strip()
    if detect_platform_from_url(text) != "soop":
        return text
    try:
        parsed = urlparse(text)
        parts = [part for part in parsed.path.split("/") if part]
        if parts:
            return f"https://play.sooplive.com/{parts[0]}"
    except Exception:
        pass
    return text


def stream_identity(url: str) -> str:
    normalized = normalize_stream_url(url)
    if detect_platform_from_url(normalized) == "soop":
        try:
            parts = [part for part in urlparse(normalized).path.split("/") if part]
            if parts:
                return f"soop:{parts[0].lower()}"
        except Exception:
            pass
    return extract_channel_id(normalized) or normalized.lower()



def wait_until_stable_for_processing(ts_path, lg, wait_sec=10):
    try:
        p = Path(ts_path)
        if not p.exists():
            return False

        size1 = p.stat().st_size
        if size1 <= 0:
            return False

        time.sleep(wait_sec)

        if not p.exists():
            return False

        size2 = p.stat().st_size
        if size1 != size2:
            try:
                lg.info(f"아직 녹화 중인 part로 보여 후처리 건너뜀: {p.name} ({size1} -> {size2})")
            except Exception:
                pass
            return False

        return True

    except Exception as e:
        try:
            lg.warning(f"part 안정성 확인 실패: {ts_path} / {e}")
        except Exception:
            pass
        return False


def safe_unlink_recording_part(path, lg, attempts=12, delay=2):
    p = Path(path)

    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            try:
                lg.info(f"로컬 ts 삭제 완료: {p.name}")
            except Exception:
                pass
            return True

        except PermissionError:
            try:
                lg.warning(f"파일이 아직 사용 중이라 삭제 재시도 {i+1}/{attempts}: {p.name}")
            except Exception:
                pass
            time.sleep(delay)

        except FileNotFoundError:
            return True

        except Exception as e:
            try:
                lg.warning(f"로컬 ts 삭제 실패: {p} / {e}")
            except Exception:
                pass
            return False

    try:
        lg.warning(f"끝까지 잠겨 있어 이번 삭제는 건너뜀. 다음 정리 루프에서 다시 처리됩니다: {p}")
    except Exception:
        pass

    return False
# CHZZK_V083_LOCK_FIX_END

# v0.8.2 핵심
# - part가 끝나면 즉시 ts -> mp4 변환 -> Google Drive parts 폴더 이동 -> 로컬 ts 삭제
# - 별도 cleanup worker가 temp에 남은 *_partNNN.ts를 계속 감시해서 자동 처리
# - 최종 병합은 메모리 목록이 아니라 Google Drive parts 폴더를 스캔해서 수행
# - 그래서 중간에 part 처리가 한 번 밀려도 방송 종료 후 병합에 포함됨

cleanup_seen: Set[str] = set()
cleanup_lock = threading.Lock()


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_config() -> dict:
    return json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8-sig"))


def load_title_filter_config(fallback: dict) -> dict:
    """Reload title rules without replacing the stable session configuration."""
    try:
        current = load_config()
        return current if isinstance(current, dict) else fallback
    except Exception:
        # A partially saved config file must never interrupt an active capture.
        return fallback


def ensure_dirs(c: dict) -> None:
    for k in ["temp_dir", "merged_dir", "logs_dir", "failed_dir"]:
        Path(c["paths"][k]).mkdir(parents=True, exist_ok=True)
    Path(c["paths"]["drive_dir"]).mkdir(parents=True, exist_ok=True)


def setup_logger(c: dict) -> logging.Logger:
    lg = logging.getLogger("chzzk_recorder")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(Path(c["paths"]["logs_dir"]) / "recorder.log", maxBytes=5*1024*1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    lg.addHandler(fh)
    lg.addHandler(ch)
    return lg


def db_connect(c: dict):
    d = c.get("database", {})
    if not d.get("enabled", False):
        return None
    return pymysql.connect(
        host=d.get("host", "127.0.0.1"),
        port=int(d.get("port", 3306)),
        user=d.get("user", "recorder"),
        password=d.get("password", ""),
        database=d.get("database", "chzzk_recorder"),
        charset=d.get("charset", "utf8mb4"),
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def db_exec(c, lg, sql, params=(), fetchone=False, fetchall=False):
    try:
        conn = db_connect(c)
        if conn is None:
            return None
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if fetchone:
                    return cur.fetchone()
                if fetchall:
                    return cur.fetchall()
                return cur.lastrowid
    except Exception as e:
        lg.warning(f"DB 오류: {e}")
        return None


def sanitize(text: str, max_len=100) -> str:
    text = str(text or "").strip()
    for ch in '<>:"/\\|?*\n\r\t':
        text = text.replace(ch, "_")
    text = re.sub(r"[\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", " ", text).strip().replace(" ", "_")
    text = text.strip(" ._")
    if len(text) > max_len:
        text = text[:max_len].strip(" ._")
    return text or "untitled"


def parse_bool(t):
    t = str(t).strip().lower()
    if t in ["1","true","yes","y","on","사용","활성"]: return True
    if t in ["0","false","no","n","off","미사용","비활성"]: return False
    return None


def extract_channel_id(url: str) -> Optional[str]:
    try:
        p = urlparse(url).path.strip("/").split("/")
        if len(p) >= 2 and p[0] == "live":
            return p[1]
    except Exception:
        pass
    return None


def fetch_title(url: str, lg: logging.Logger, quiet: bool = False) -> Optional[str]:
    if detect_platform_from_url(url) == "soop":
        helper = Path(__file__).with_name("soop_agent_recorder.py")
        try:
            p = subprocess.run(
                [sys.executable, str(helper), normalize_stream_url(url), "--probe"],
                capture_output=True, text=True, timeout=40,
                encoding="utf-8", errors="ignore",
            )
            lines = [line for line in (p.stdout or "").splitlines() if line.strip()]
            payload = json.loads(lines[-1]) if lines else {}
            return str(payload.get("title") or "").strip() or None
        except Exception as e:
            if not quiet:
                lg.warning(f"SOOP 방송 제목 조회 오류: {e}")
            return None
    cid = extract_channel_id(url)
    if not cid:
        return None
    headers = {"User-Agent":"Mozilla/5.0", "Accept":"application/json", "Referer":f"https://chzzk.naver.com/live/{cid}"}
    urls = [
        f"https://api.chzzk.naver.com/service/v1/channels/{cid}/live-detail",
        f"https://api.chzzk.naver.com/service/v2/channels/{cid}/live-detail",
        f"https://api.chzzk.naver.com/polling/v2/channels/{cid}/live-status",
    ]
    def pick(o):
        if not isinstance(o, dict): return None
        content = o.get("content")
        if isinstance(content, dict):
            for k in ["liveTitle","title","broadcastTitle"]:
                if content.get(k): return str(content[k])
            live = content.get("live")
            if isinstance(live, dict):
                for k in ["liveTitle","title","broadcastTitle"]:
                    if live.get(k): return str(live[k])
        for k in ["liveTitle","title","broadcastTitle"]:
            if o.get(k): return str(o[k])
        return None
    for u in urls:
        try:
            r = requests.get(u, headers=headers, timeout=10)
            if not r.ok:
                if not quiet:
                    lg.warning(f"방송 제목 조회 실패 HTTP {r.status_code}: {u}")
                continue
            title = pick(r.json())
            if title:
                if not quiet:
                    lg.info(f"방송 제목 조회 성공: {title}")
                return title
        except Exception as e:
            if not quiet:
                lg.warning(f"방송 제목 조회 오류: {u} / {e}")
    return None


def session_base(name: str, title: Optional[str] = None) -> str:
    """
    방송 제목은 방송 중에도 바뀔 수 있으므로 세션 ID/파일명 기준에서 제외합니다.
    제목은 대시보드 표시용으로만 사용하고, 세션은 시작 시각 + 스트리머명으로 고정합니다.

    예전 형식: 2026-06-02_방송제목_스트리머_22-37-52
    새 형식:   2026-06-02_스트리머_22-37-52
    """
    t = datetime.now()
    return f"{t:%Y-%m-%d}_{sanitize(name,40)}_{t:%H-%M-%S}"


def read_streamers(c: dict) -> List[dict]:
    p = Path(__file__).with_name("streamers.txt")
    if not p.exists(): p.write_text("", encoding="utf-8")
    default_q = c["recording"].get("default_quality", "best")
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"): continue
        parts = [x.strip() for x in raw.split(",")]
        if len(parts) >= 4 and parse_bool(parts[0]) is not None:
            enabled, name, url, quality = parse_bool(parts[0]), parts[1], parts[2], parts[3] or default_q
        elif len(parts) >= 2:
            enabled, name, url, quality = True, parts[0], parts[1], parts[2] if len(parts) >= 3 and parts[2] else default_q
        else:
            continue
        if enabled and name and url:
            url = normalize_stream_url(url)
            platform = detect_platform_from_url(url)
            if platform not in {"chzzk", "soop"}:
                continue
            out.append({"name": name, "url": url, "quality": quality, "platform": platform, "line_no": i})
    # STREAMER_LIVEID_DEDUPE_PATCH
    # ?? CHZZK LiveID? streamers.txt? ?? ????? ??? ??/?????.
    deduped = []
    seen_live_ids = set()

    for item in out:
        live_id = stream_identity(item.get("url", ""))

        if live_id in seen_live_ids:
            try:
                print(f"[STREAMER_DEDUPE] skip duplicate LiveID={live_id} name={item.get('name')}", flush=True)
            except Exception:
                pass
            continue

        seen_live_ids.add(live_id)
        deduped.append(item)

    return deduped


def streamer_enabled_in_file(name: str) -> bool:
    """Return False only when the streamer is explicitly disabled or removed."""
    p = Path(__file__).with_name("streamers.txt")
    try:
        if not p.exists():
            return True
        for line in p.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = [x.strip() for x in raw.split(",")]
            if len(parts) < 2:
                continue
            enabled = parse_bool(parts[0])
            if enabled is None:
                row_name = parts[0]
                row_enabled = True
            else:
                row_name = parts[1]
                row_enabled = enabled
            if row_name == name:
                return bool(row_enabled)
        # Removing a streamer from the file has the same stop semantics as OFF.
        return False
    except Exception:
        # A temporary sharing/read error must never terminate a healthy recording.
        return True


def stream_is_live(s: dict, lg: logging.Logger) -> dict:
    url = normalize_stream_url(s["url"])
    platform = s.get("platform") or detect_platform_from_url(url)
    if platform == "soop":
        st = time.time()
        # An actively growing official-agent journal is stronger evidence than
        # SOOP's public Streamlink endpoint, which can incorrectly return OFF.
        temp_dir = Path(__file__).with_name("temp")
        try:
            for state_path in sorted(
                temp_dir.glob("*.soop.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            ):
                if time.time() - state_path.stat().st_mtime > 90:
                    break
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if normalize_stream_url(str(state.get("url") or "")) != url:
                    continue
                if int(state.get("video_frames") or 0) > 0 and float(state.get("elapsed_sec") or 0) > 0:
                    return {
                        "name": s["name"], "url": url, "quality": s["quality"],
                        "is_live": True, "check_ok": True,
                        "elapsed_sec": round(time.time() - st, 2),
                        "checked_at": now_text(), "error": "",
                        "live_title": "", "live_source": "capture_heartbeat",
                    }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

        helper = Path(__file__).with_name("soop_agent_recorder.py")
        cmd = [sys.executable, str(helper), url, "--probe"]
        try:
            p = subprocess.run(
                cmd, capture_output=True, text=True, timeout=45,
                encoding="utf-8", errors="ignore",
            )
            lines = [line for line in (p.stdout or "").splitlines() if line.strip()]
            payload = json.loads(lines[-1]) if lines else {}
            live = bool(payload.get("is_live"))
            check_ok = bool(payload.get("check_ok", False))
            error = str(payload.get("error") or (p.stderr or "")[-500:])
            return {
                "name": s["name"], "url": url, "quality": s["quality"],
                "is_live": live, "check_ok": check_ok,
                "elapsed_sec": round(time.time() - st, 2),
                "checked_at": now_text(), "error": error,
                "live_title": payload.get("title") or "",
                "live_source": "soop_player",
            }
        except Exception as e:
            return {
                "name": s["name"], "url": url, "quality": s["quality"],
                "is_live": False, "check_ok": False,
                "elapsed_sec": round(time.time() - st, 2),
                "checked_at": now_text(), "error": str(e),
            }
    cmd = [sys.executable, "-m", "streamlink", "--stream-url", url, s["quality"]]
    st = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=25, encoding="utf-8", errors="ignore")
        live = p.returncode == 0 and bool(p.stdout.strip())
        error = "" if live else (p.stderr[-500:] if p.stderr else "")
        error_text = error.lower()
        transient_markers = (
            "timed out", "timeout", "connection", "network", "temporarily",
            "remote end closed", "name resolution", "dns", "429", "502", "503", "504",
        )
        check_ok = live or not any(marker in error_text for marker in transient_markers)
        return {"name":s["name"], "url":url, "quality":s["quality"], "is_live":live, "check_ok":check_ok, "elapsed_sec":round(time.time()-st,2), "checked_at":now_text(), "error":error}
    except Exception as e:
        return {"name":s["name"], "url":url, "quality":s["quality"], "is_live":False, "check_ok":False, "elapsed_sec":round(time.time()-st,2), "checked_at":now_text(), "error":str(e)}


def confirm_stream_offline(s: dict, c: dict, lg: logging.Logger) -> bool:
    """
    streamlink가 자연 종료되더라도 바로 방송 종료로 확정하지 않습니다.
    방제 변경, CHZZK 순간 끊김, 네트워크 흔들림 때 streamlink가 한 번 끝날 수 있어서
    여러 번 연속으로 OFF 확인이 될 때만 최종 병합 단계로 넘어갑니다.
    """
    rec_cfg = c.get("recording", {})
    checks = max(1, int(rec_cfg.get("offline_confirm_checks", 3)))
    interval = max(5, int(rec_cfg.get("offline_confirm_interval_sec", 30)))

    for i in range(checks):
        r = stream_is_live(s, lg)
        if r.get("is_live"):
            lg.info(f"[{s['name']}] 오프라인 확인 {i+1}/{checks}: 다시 LIVE 감지. 기존 세션 유지")
            return False

        if not r.get("check_ok", True):
            err = (r.get("error") or "상태 조회 실패").replace("\n", " ").strip()
            lg.warning(f"[{s['name']}] 상태 조회 오류는 OFF로 세지 않습니다. 기존 세션 유지: {err[:180]}")
            return False

        err = (r.get("error") or "").replace("\n", " ").strip()
        if err:
            lg.info(f"[{s['name']}] 오프라인 확인 {i+1}/{checks}: OFF/응답 없음 - {err[:160]}")
        else:
            lg.info(f"[{s['name']}] 오프라인 확인 {i+1}/{checks}: OFF")

        if i < checks - 1:
            time.sleep(interval)

    lg.info(f"[{s['name']}] {checks}회 연속 OFF 확인. 방송 종료로 확정")
    return True


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path.anchor or str(path)).free / (1024**3)


def folder_size_gb(path: Path) -> float:
    if not path.exists(): return 0.0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file(): total += f.stat().st_size
        except Exception:
            pass
    return total / (1024**3)


def write_status(c: dict, status: dict) -> None:
    logs = Path(c["paths"]["logs_dir"])
    tmp = logs / "status.tmp"
    dst = logs / "status.json"
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(dst)


def append_history(c: dict, event: dict) -> None:
    event["time"] = now_text()
    with (Path(c["paths"]["logs_dir"]) / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def stop_proc(p: subprocess.Popen, lg: logging.Logger, wait_timeout: int = 12):
    try:
        if os.name == "nt": p.send_signal(signal.CTRL_BREAK_EVENT)
        else: p.send_signal(signal.SIGINT)
        p.wait(timeout=max(12, int(wait_timeout)))
    except Exception:
        try:
            p.terminate(); p.wait(timeout=8)
        except Exception:
            try: p.kill()
            except Exception: pass





def record_part(s: dict, ts_path: Path, duration: int, lg: logging.Logger,
                title_filter_stop: Optional[threading.Event] = None) -> str:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    stream_url = normalize_stream_url(s["url"])
    is_soop = (s.get("platform") or detect_platform_from_url(stream_url)) == "soop"
    using_soop_agent = is_soop

    if using_soop_agent:
        helper = Path(__file__).with_name("soop_agent_recorder.py")
        cmd = [sys.executable, str(helper), stream_url, str(ts_path)]
        lg.info(f"[{s['name']}] SOOP 공식 에이전트 원본화질 녹화를 시작합니다.")
    else:
        cmd = [sys.executable, "-m", "streamlink", stream_url, s["quality"], "-o", str(ts_path)]

    p = subprocess.Popen(
        cmd,
        text=True,
        encoding="utf-8",
        errors="ignore",
        creationflags=flags,
    )
    # SOOP's already-open player can remain authorized when a streamer changes
    # the room to password-protected mode.  Do not tear that player down at the
    # ordinary one-hour part boundary: leaving/re-entering would require the
    # password even though the existing viewer session may keep receiving media.
    # CHZZK retains the existing fixed-duration part rotation.
    deadline = None if using_soop_agent else time.monotonic() + duration
    if using_soop_agent:
        lg.info(
            f"[{s['name']}] SOOP 연속 녹화 모드: 방송 종료까지 플레이어와 연결을 유지합니다."
        )
    while True:
        if title_filter_stop is not None and title_filter_stop.is_set():
            lg.info(f"[{s['name']}] 방송 제목 필터 불일치 감지. 현재 녹화를 안전 종료합니다.")
            stop_proc(p, lg, 240 if using_soop_agent else 12)
            return "title_filter_stop"

        if not streamer_enabled_in_file(s["name"]):
            lg.info(f"[{s['name']}] 스트리머 관리에서 OFF 감지. 현재 녹화를 안전 종료합니다.")
            stop_proc(p, lg, 240 if using_soop_agent else 12)
            return "manual_stop"

        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            lg.info(f"[{s['name']}] 분할 시간 도달. 현재 part 마감")
            stop_proc(p, lg, 12)
            return "time_split"

        try:
            wait_timeout = 2.0 if remaining is None else min(2.0, remaining)
            code = p.wait(timeout=wait_timeout)
            output_ok = ts_path.exists() and ts_path.stat().st_size >= 256 * 1024
            if using_soop_agent:
                # The official SOOP agent can disconnect when a live room is
                # switched to password-protected mode.  The current player
                # remains on the broadcast route, so preserve this part and
                # let the session loop open the next part with the same
                # authenticated browser profile instead of finalizing the
                # recording or falling back to public Streamlink.
                try:
                    probe = subprocess.run(
                        [sys.executable, str(helper), stream_url, "--probe"],
                        capture_output=True, text=True, timeout=40,
                        encoding="utf-8", errors="ignore",
                    )
                    lines = [line for line in (probe.stdout or "").splitlines() if line.strip()]
                    payload = json.loads(lines[-1]) if lines else {}
                    if payload.get("check_ok") and payload.get("is_live"):
                        lg.warning(
                            f"[{s['name']}] SOOP 공식 에이전트 연결이 끊겼지만 현재 방송방은 LIVE입니다. "
                            f"현재 part를 보존하고 다음 part로 인증 세션 재연결을 시도합니다. "
                            f"reason={payload.get('live_reason') or 'soop_player_live'}"
                        )
                        return "soop_reconnect"
                    if code == 0 and output_ok:
                        # A complete part followed by a definite OFF response
                        # is a normal broadcast end; the existing confirmation
                        # path still performs the configured repeated checks.
                        pass
                except Exception as exc:
                    lg.warning(f"[{s['name']}] SOOP 상태 재확인 실패: {exc}")

            if using_soop_agent and (code != 0 or not output_ok):
                # Do not start the legacy fallback when SOOP's own player has
                # already moved to /null.  That fallback cannot create media
                # while offline and used to generate one empty DB part every
                # retry cycle for hours.
                try:
                    probe = subprocess.run(
                        [sys.executable, str(helper), stream_url, "--probe"],
                        capture_output=True, text=True, timeout=40,
                        encoding="utf-8", errors="ignore",
                    )
                    lines = [line for line in (probe.stdout or "").splitlines() if line.strip()]
                    payload = json.loads(lines[-1]) if lines else {}
                    if payload.get("check_ok") and not payload.get("is_live"):
                        lg.info(
                            f"[{s['name']}] SOOP 공식 플레이어 OFF 확인. "
                            "빈 fallback 녹화를 만들지 않고 종료 확인으로 이동합니다."
                        )
                        return "stream_ended"
                except Exception as exc:
                    lg.warning(f"[{s['name']}] SOOP 종료 보조 확인 실패, 기존 fallback 사용: {exc}")
                lg.warning(
                    f"[{s['name']}] SOOP 원본화질 경로를 열지 못했습니다(code={code}). "
                    "녹화 누락을 막기 위해 기존 방식으로 자동 전환합니다."
                )
                fallback_cmd = [
                    sys.executable,
                    "-m",
                    "streamlink",
                    stream_url,
                    s["quality"],
                    "-o",
                    str(ts_path),
                ]
                p = subprocess.Popen(
                    fallback_cmd,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=flags,
                )
                using_soop_agent = False
                continue
            source_name = "SOOP agent recorder" if using_soop_agent else "streamlink"
            lg.info(f"[{s['name']}] {source_name} 자연 종료: code={code}")
            return "stream_ended"
        except subprocess.TimeoutExpired:
            continue


def ffmpeg_copy(src: Path, dst: Path, lg: logging.Logger) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    lg.info(f"ffmpeg 변환 시작: {src.name} -> {dst.name}")
    p = subprocess.run(["ffmpeg","-y","-i",str(src),"-c","copy",str(dst)], capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if p.returncode != 0:
        lg.error("ffmpeg 변환 실패")
        lg.error(p.stderr[-2000:])
        return False
    if not dst.exists() or dst.stat().st_size <= 0:
        lg.error("mp4 파일이 없거나 0바이트")
        return False
    if not validate_converted_media(src, dst, lg):
        lg.error(f"MP4 검증 실패, TS 원본을 보존합니다: {src.name}")
        return False
    lg.info(f"ffmpeg 변환 완료: {dst}")
    return True


def media_duration(path: Path) -> Optional[float]:
    try:
        p = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=90,
        )
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
        if not m:
            return None
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return None


def packet_scan_ok(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    try:
        p = subprocess.run(
            ["ffmpeg", "-v", "error", "-err_detect", "ignore_err", "-i", str(path),
             "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-f", "null", null_target],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
        )
        return p.returncode == 0
    except Exception:
        return False


def validate_converted_media(src: Path, dst: Path, lg: logging.Logger) -> bool:
    if not packet_scan_ok(dst):
        lg.error(f"MP4 전체 패킷 검사 실패: {dst.name}")
        return False
    src_duration = media_duration(src)
    dst_duration = media_duration(dst)
    if src_duration and (not dst_duration or dst_duration < src_duration * 0.97):
        lg.error(f"MP4 재생시간 부족: src={src_duration:.1f}s / mp4={dst_duration or 0:.1f}s")
        return False
    if src_duration and dst_duration:
        lg.info(f"MP4 검증 완료: src={src_duration:.1f}s / mp4={dst_duration:.1f}s")
    return True


def make_concat(parts: List[Path], list_path: Path):
    def q(p): return str(p).replace("\\", "/").replace("'", "'\\''")
    list_path.write_text("\n".join([f"file '{q(p)}'" for p in parts]) + "\n", encoding="utf-8")


def ffmpeg_concat(parts: List[Path], final_path: Path, list_path: Path, lg: logging.Logger, config=None) -> bool:
    # Use the same loss-minimizing merge path as the background Merge Worker.
    try:
        from drive_parts_merge_worker import ffmpeg_concat as resilient_concat
        lg.info("복구형 공용 병합 로직을 사용합니다.")
        return bool(resilient_concat(parts, final_path, list_path, config))
    except Exception as e:
        lg.exception(f"복구형 공용 병합 호출 오류: {e}")
        return False


def db_create_session(c, lg, sid, s, start_at, parts_dir):
    db_exec(c, lg, "INSERT INTO recording_sessions (session_id,streamer_name,source_url,quality,started_at,status,parts_dir) VALUES (%s,%s,%s,%s,%s,'recording',%s)", (sid,s["name"],s["url"],s["quality"],start_at,str(parts_dir)))


def db_update_session(c, lg, sid, **kw):
    fields, vals = [], []
    for k,v in kw.items():
        if v == "NOW()": fields.append(f"{k}=NOW()")
        else: fields.append(f"{k}=%s"); vals.append(v)
    if not fields: return
    vals.append(sid)
    db_exec(c, lg, f"UPDATE recording_sessions SET {', '.join(fields)} WHERE session_id=%s", vals)


def db_add_part(c, lg, sid, name, idx, ts_path):
    return db_exec(c, lg, "INSERT INTO recording_parts (session_id,streamer_name,part_index,started_at,status,ts_path) VALUES (%s,%s,%s,NOW(),'recording',%s)", (sid,name,idx,str(ts_path)))


def db_find_part(c, lg, sid, idx):
    return db_exec(c, lg, "SELECT * FROM recording_parts WHERE session_id=%s AND part_index=%s LIMIT 1", (sid,idx), fetchone=True)


def db_update_part(c, lg, part_id, **kw):
    if not part_id: return
    fields, vals = [], []
    for k,v in kw.items():
        if v == "NOW()": fields.append(f"{k}=NOW()")
        else: fields.append(f"{k}=%s"); vals.append(v)
    if not fields: return
    vals.append(part_id)
    db_exec(c, lg, f"UPDATE recording_parts SET {', '.join(fields)} WHERE id=%s", vals)


def db_insert_final(c, lg, data):
    db_exec(c, lg, """
        INSERT INTO recordings
        (streamer_name,broadcast_title,source_url,quality,started_at,ended_at,status,temp_path,final_path,file_size_mb,youtube_status,parts_dir,parts_count,session_id)
        VALUES (%s,%s,%s,%s,%s,NOW(),'done','',%s,%s,%s,%s,%s,%s)
    """, (data["streamer_name"],data.get("broadcast_title"),data["source_url"],data["quality"],data["started_at"],data["final_path"],data["file_size_mb"],data["youtube_status"],data["parts_dir"],data["parts_count"],data["session_id"]))


def normalize_title_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def load_session_title_history(parts_dir: Path) -> List[str]:
    """Load title history written by this or an older recorder version."""
    try:
        meta_path = parts_dir / "_session_meta.json"
        if not meta_path.is_file():
            return []
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw_titles = meta.get("broadcast_titles")
        if not isinstance(raw_titles, list):
            raw_titles = [meta.get("broadcast_title")]
        result = []
        seen = set()
        for raw in raw_titles:
            title = normalize_title_text(raw)
            key = title.casefold()
            if title and key not in seen:
                seen.add(key)
                result.append(title)
        return result
    except Exception:
        return []


def write_session_metadata(parts_dir: Path, streamer_name: str, broadcast_title: str,
                           source_url: str, lg, broadcast_titles=None):
    try:
        titles = []
        seen = set()
        for raw in (broadcast_titles or [broadcast_title]):
            title = normalize_title_text(raw)
            key = title.casefold()
            if title and key not in seen:
                seen.add(key)
                titles.append(title)
        combined_title = " / ".join(titles) or normalize_title_text(broadcast_title)
        meta = {
            "streamer_name": streamer_name,
            "broadcast_title": combined_title,
            "broadcast_titles": titles,
            "source_url": source_url,
            "captured_at": now_text(),
        }
        tmp = parts_dir / "_session_meta.tmp"
        dst = parts_dir / "_session_meta.json"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(dst)
    except Exception as e:
        lg.warning(f"[{streamer_name}] 방송 제목 메타데이터 저장 실패: {e}")


def parse_part_name(ts_path: Path):
    m = re.match(r"(.+)_part(\d{3,})\.ts$", ts_path.name)
    if not m: return None, None
    return m.group(1), int(m.group(2))


def session_info(c, lg, sid):
    return db_exec(c, lg, "SELECT * FROM recording_sessions WHERE session_id=%s LIMIT 1", (sid,), fetchone=True)


def process_part(c, lg, sid, name, idx, ts_path, parts_dir, part_id=None,
                 empty_status="missing_ts") -> bool:
    with cleanup_lock:
        key = str(ts_path.resolve()).lower()
        if key in cleanup_seen:
            return False
        cleanup_seen.add(key)
    try:
        if not ts_path.exists() or ts_path.stat().st_size <= 0:
            if part_id:
                if empty_status == "discarded_empty":
                    error_message = "방송 종료 감지 과정에서 생성된 빈 시도; 병합 대상 제외"
                else:
                    error_message = "ts 없음"
                db_update_part(
                    c, lg, part_id, ended_at="NOW()",
                    status=empty_status, error_message=error_message,
                )
            return False
        size1 = ts_path.stat().st_size
        time.sleep(2)
        if not ts_path.exists() or ts_path.stat().st_size != size1:
            # 아직 쓰는 중이면 다음 스캔에서 다시 처리
            with cleanup_lock: cleanup_seen.discard(key)
            return False
        size_mb = ts_path.stat().st_size / (1024*1024)
        lg.info(f"[{name}] part{idx:03d} 후처리 시작: {ts_path.name} / {size_mb:.2f} MB")
        if size_mb < float(c["recording"].get("min_file_size_mb", 5)):
            failed = Path(c["paths"]["failed_dir"]) / ts_path.name
            shutil.move(str(ts_path), str(failed))
            if part_id: db_update_part(c, lg, part_id, ended_at="NOW()", status="failed_small_file", file_size_mb=round(size_mb,2), error_message=str(failed))
            return False
        local_mp4 = Path(c["paths"]["merged_dir"]) / f"{ts_path.stem}.mp4"
        drive_mp4 = Path(parts_dir) / f"{ts_path.stem}.mp4"
        if drive_mp4.exists() and drive_mp4.stat().st_size > 0:
            if validate_converted_media(ts_path, drive_mp4, lg):
                lg.info(f"[{name}] part{idx:03d} 검증된 Drive 파일 이미 존재. ts를 삭제합니다: {drive_mp4}")
                safe_unlink_recording_part(ts_path, lg)
                if part_id: db_update_part(c, lg, part_id, ended_at="NOW()", status="done", drive_path=str(drive_mp4), file_size_mb=round(size_mb,2))
                return True
            invalid = drive_mp4.with_name(f"{drive_mp4.stem}.invalid_{datetime.now():%Y%m%d_%H%M%S}{drive_mp4.suffix}")
            drive_mp4.replace(invalid)
            lg.warning(f"[{name}] 기존 Drive MP4 검증 실패로 격리: {invalid}")
        if not ffmpeg_copy(ts_path, local_mp4, lg):
            if part_id: db_update_part(c, lg, part_id, ended_at="NOW()", status="ffmpeg_failed", file_size_mb=round(size_mb,2))
            with cleanup_lock: cleanup_seen.discard(key)
            return False
        Path(parts_dir).mkdir(parents=True, exist_ok=True)
        shutil.move(str(local_mp4), str(drive_mp4))
        if not validate_converted_media(ts_path, drive_mp4, lg):
            if part_id: db_update_part(c, lg, part_id, ended_at="NOW()", status="drive_verify_failed", file_size_mb=round(size_mb,2), error_message=str(drive_mp4))
            with cleanup_lock: cleanup_seen.discard(key)
            return False
        safe_unlink_recording_part(ts_path, lg)
        lg.info(f"[{name}] part{idx:03d} Drive 이동 + 로컬 ts 삭제 완료: {drive_mp4}")
        if part_id: db_update_part(c, lg, part_id, ended_at="NOW()", status="done", mp4_path=str(local_mp4), drive_path=str(drive_mp4), file_size_mb=round(size_mb,2))
        append_history(c, {"event":"part_done", "streamer":name, "session_id":sid, "part_index":idx, "drive_path":str(drive_mp4), "size_mb":round(size_mb,2)})
        return True
    except Exception as e:
        lg.exception(f"[{name}] part{idx:03d} 후처리 오류: {e}")
        with cleanup_lock: cleanup_seen.discard(key)
        return False


def cleanup_worker(c, lg, stop):
    temp = Path(c["paths"]["temp_dir"])
    lg.info("part cleanup worker 시작")
    while not stop["v"]:
        try:
            for ts in sorted(temp.glob("*_part*.ts")):
                sid, idx = parse_part_name(ts)
                if not sid: continue
                info = session_info(c, lg, sid)
                if not info: continue
                parts_dir = info.get("parts_dir")
                name = info.get("streamer_name") or "unknown"
                if not parts_dir: continue
                part = db_find_part(c, lg, sid, idx)
                part_id = part.get("id") if part else None
                if part and part.get("status") == "done":
                    if ts.exists(): safe_unlink_recording_part(ts, lg)
                    continue
                process_part(c, lg, sid, name, idx, ts, Path(parts_dir), part_id)
        except Exception as e:
            lg.warning(f"cleanup worker 오류: {e}")
        time.sleep(int(c["recording"].get("part_cleanup_interval_sec", 20)))
    lg.info("part cleanup worker 종료")



def _to_datetime(value):
    """DB/Python/문자열 started_at 값을 datetime으로 최대한 안전하게 변환합니다."""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:19], fmt)
        except Exception:
            pass
    return None


def find_resume_session(c, lg, streamer_name: str, max_age_hours: int = 18):
    """
    녹화기를 껐다 켜도 같은 방송 세션을 이어 쓰기 위한 복구 로직입니다.

    기존 문제:
    - 프로그램 재시작 시 메모리 active 목록이 날아감
    - 같은 방송인데도 새 session_id가 만들어짐
    - 결과적으로 part001 세션이 여러 개 생김

    새 동작:
    - DB recording_sessions에 아직 status='recording'인 같은 스트리머 세션이 있고
    - 너무 오래된 세션이 아니며
    - parts_dir가 실제로 존재하면
    - 그 세션을 이어 받아 다음 part 번호부터 녹화합니다.
    """
    max_age_hours = max(1, int(max_age_hours or 18))
    row = db_exec(
        c,
        lg,
        """
        SELECT *
        FROM recording_sessions
        WHERE streamer_name=%s
          AND status='recording'
          AND (final_path IS NULL OR final_path='')
        ORDER BY started_at ASC
        LIMIT 1
        """,
        (streamer_name,),
        fetchone=True,
    )
    if not row:
        return None

    started_dt = _to_datetime(row.get("started_at"))
    if started_dt and datetime.now() - started_dt > timedelta(hours=max_age_hours):
        lg.info(f"[{streamer_name}] 재개 후보가 너무 오래됨({row.get('started_at')}). 새 세션 시작")
        return None

    sid = row.get("session_id")
    parts_dir = row.get("parts_dir")
    if not sid or not parts_dir:
        return None

    parts_path = Path(parts_dir)
    if not parts_path.exists():
        lg.info(f"[{streamer_name}] 재개 후보 parts_dir 없음: {parts_path}. 새 세션 시작")
        return None

    return row


def max_existing_part_index(c, lg, sid: str, base: str, temp: Path, parts_dir: Path) -> int:
    """DB + temp + Drive parts 폴더를 모두 보고 다음 part 번호를 계산합니다."""
    max_idx = 0

    row = db_exec(
        c,
        lg,
        "SELECT MAX(part_index) AS max_idx FROM recording_parts WHERE session_id=%s",
        (sid,),
        fetchone=True,
    )
    try:
        if row and row.get("max_idx") is not None:
            max_idx = max(max_idx, int(row.get("max_idx") or 0))
    except Exception:
        pass

    patterns = [f"{base}_part*.ts", f"{base}_part*.mp4"]
    for folder in [temp, parts_dir]:
        try:
            for pattern in patterns:
                for f in folder.glob(pattern):
                    m = re.search(r"_part(\d{3,})\.(ts|mp4)$", f.name)
                    if m:
                        max_idx = max(max_idx, int(m.group(1)))
        except Exception:
            pass

    return max_idx


def session_worker(s, c, active, lock, lg):
    name = s["name"]
    title = s.get("_initial_live_title") or fetch_title(s["url"], lg)
    seg = int(c["recording"].get("segment_duration_sec", 3600))
    post_delay = int(c["recording"].get("segment_post_check_delay_sec", 8))
    resume_hours = int(c["recording"].get("resume_open_session_hours", 18))
    temp = Path(c["paths"]["temp_dir"])
    drive_root = Path(c["paths"]["drive_dir"])

    resume = find_resume_session(c, lg, name, resume_hours)
    if resume:
        sid = str(resume["session_id"])
        base = sid
        parts_dir = Path(resume["parts_dir"])
        streamer_dir = parts_dir.parent
        started_at = str(resume.get("started_at") or now_text())
        idx = max_existing_part_index(c, lg, sid, base, temp, parts_dir)
        parts_dir.mkdir(parents=True, exist_ok=True)
        db_update_session(c, lg, sid, status="recording")
        lg.info("="*60)
        lg.info(f"[{name}] 기존 녹화 세션 재개 / 다음 part: {idx+1:03d}")
        lg.info(f"[{name}] 세션ID: {sid}")
        lg.info(f"[{name}] parts_dir: {parts_dir}")
        lg.info(f"[{name}] 제목: {title or name}")
        lg.info("="*60)
    else:
        base = session_base(name, title)
        sid = base
        streamer_dir = drive_root / sanitize(name,40) / datetime.now().strftime("%Y-%m")
        parts_dir = streamer_dir / f"{base}_parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        started_at = now_text()
        idx = 0
        db_create_session(c, lg, sid, s, started_at, parts_dir)
        lg.info("="*60)
        lg.info(f"[{name}] 새 분할 녹화 세션 시작 / {seg}초 단위")
        lg.info(f"[{name}] 세션ID: {sid} (방송 제목 제외 고정)")
        lg.info(f"[{name}] 제목: {title or name}")
        lg.info("="*60)

    title_lock = threading.Lock()
    title_history = load_session_title_history(parts_dir)

    def remember_title(raw_title, announce=False):
        new_title = normalize_title_text(raw_title)
        if not new_title:
            return False
        with title_lock:
            known = {item.casefold() for item in title_history}
            if new_title.casefold() in known:
                changed = False
            else:
                title_history.append(new_title)
                changed = True
            combined = " / ".join(title_history)
            snapshot = list(title_history)
        with lock:
            if name in active:
                active[name]["live_title"] = new_title
                active[name]["title_history"] = snapshot
        if changed:
            write_session_metadata(
                parts_dir, name, combined, s["url"], lg,
                broadcast_titles=snapshot,
            )
            if announce:
                lg.info(f"[{name}] 방송 제목 변경 감지: {new_title}")
                lg.info(f"[{name}] 누적 방송 제목: {combined}")
        return changed

    with lock:
        active[name] = {"name":name, "url":s["url"], "quality":s["quality"], "started_at":started_at, "ts_path":"", "live_title":title or "", "title_history":list(title_history), "session_id":sid, "parts_count":idx, "mode":"split_merge"}
    remember_title(title)
    with title_lock:
        initial_titles = list(title_history)
        initial_combined_title = " / ".join(initial_titles)
    write_session_metadata(
        parts_dir, name, initial_combined_title, s["url"], lg,
        broadcast_titles=initial_titles,
    )

    title_monitor_stop = threading.Event()
    title_filter_stop = threading.Event()
    title_check_interval = max(30, int(c["recording"].get("title_check_interval_sec", 60)))

    def title_monitor():
        while not title_monitor_stop.is_set():
            try:
                current_title = fetch_title(s["url"], lg, quiet=True)
                if current_title and title_is_definitive(s, current_title):
                    remember_title(current_title, announce=True)
                    filter_config = load_title_filter_config(c)
                    current_keywords = required_title_keywords(filter_config, s)
                    if current_keywords and not title_is_allowed(filter_config, s, current_title):
                        required = " / ".join(current_keywords)
                        lg.info(
                            f"[{name}] 방송 제목에서 필수 단어({required})가 빠졌습니다: "
                            f"{normalize_title_text(current_title)}"
                        )
                        title_filter_stop.set()
                        return
            except Exception as exc:
                lg.warning(f"[{name}] 방송 제목 변경 확인 오류: {exc}")
            if title_monitor_stop.wait(title_check_interval):
                return

    title_monitor_thread = threading.Thread(
        target=title_monitor,
        daemon=True,
        name=f"title-monitor-{sanitize(name, 30)}",
    )
    title_monitor_thread.start()
    # ASYNC_PART_POSTPROCESS_PATCH_START
    # time_split?? ?? part? ???? ???? ?? ?????? ????.
    # ?? ??? ?? part? ?? ?????.
    postprocess_threads = []

    def start_part_postprocess_async(_idx, _ts, _part_id):
        t = threading.Thread(
            target=process_part,
            args=(c, lg, sid, name, _idx, _ts, parts_dir, _part_id),
            daemon=False,
        )
        t.start()
        postprocess_threads.append(t)
        lg.info(f"[{name}] part{_idx:03d} ??? ????? ??: {_ts.name}")

    try:
        while True:
            idx += 1
            part_name = f"{base}_part{idx:03d}"
            ts = temp / f"{part_name}.ts"
            part_id = db_add_part(c, lg, sid, name, idx, ts)
            with lock:
                if name in active:
                    active[name]["ts_path"] = str(ts)
                    active[name]["parts_count"] = idx
            lg.info(f"[{name}] part{idx:03d} 녹화 시작: {ts.name}")
            reason = record_part(s, ts, seg, lg, title_filter_stop)

            if reason == "time_split":
                start_part_postprocess_async(idx, ts, part_id)
                continue

            # ?? ?? ?? ??? ?? ??/?? ?? ?? ?? part? ??? ?????.
            # A helper that exits without creating any media is a probe-like
            # terminal attempt, not a missing hour of footage.  Keep the row
            # for audit, but explicitly exclude it from completeness checks.
            empty_status = (
                "discarded_empty"
                if reason in {"stream_ended", "soop_reconnect", "title_filter_stop"} and (not ts.exists() or ts.stat().st_size <= 0)
                else "missing_ts"
            )
            process_part(
                c, lg, sid, name, idx, ts, parts_dir, part_id,
                empty_status=empty_status,
            )

            if reason == "soop_reconnect":
                lg.info(
                    f"[{name}] SOOP 방송방이 계속 LIVE라 현재 세션을 유지합니다. "
                    "다음 part에서 공식 인증 플레이어 재연결을 시도합니다."
                )
                continue

            if reason == "manual_stop":
                lg.info(f"[{name}] OFF 전환으로 녹화 종료. 완료된 part 병합 단계로 이동합니다.")
                break

            if reason == "title_filter_stop":
                lg.info(f"[{name}] 제목 필터 조건 해제. 완료된 part 병합 단계로 이동합니다.")
                break

            if reason == "stream_ended":
                time.sleep(post_delay)
                if confirm_stream_offline(s, c, lg):
                    lg.info(f"[{name}] 방송 종료로 판단. 최종 병합 시작")
                    break
                lg.info(f"[{name}] 일시 끊김/방제 변경 가능성. 기존 세션으로 다음 part 계속")
        # 모든 비동기 파트 후처리가 끝나야 마지막 파트가 누락되지 않습니다.
        wait_deadline = time.time() + int(c["recording"].get("postprocess_wait_timeout_sec", 3600))
        while True:
            pending_threads = [t for t in postprocess_threads if t.is_alive()]
            if not pending_threads:
                break
            if time.time() >= wait_deadline:
                db_update_session(c, lg, sid, ended_at="NOW()", status="postprocess_timeout",
                                  parts_count=len(list(parts_dir.glob(f"{base}_part*.mp4"))),
                                  error_message=f"파트 후처리 {len(pending_threads)}개 시간 초과; 원본 보존")
                lg.error(f"[{name}] 파트 후처리 시간 초과. 병합하지 않고 원본을 보존합니다.")
                return
            lg.info(f"[{name}] 파트 후처리 완료 대기: {len(pending_threads)}개")
            for t in pending_threads:
                t.join(timeout=5)
        parts = sorted(parts_dir.glob(f"{base}_part*.mp4"))
        if not parts:
            db_update_session(c, lg, sid, ended_at="NOW()", status="no_parts", parts_count=0, error_message="병합할 part 없음")
            return
        final_path = streamer_dir / f"{base}.mp4"
        db_update_session(c, lg, sid, status="merging", parts_count=len(parts))
        part_indices = {int(m.group(1)) for p in parts
                        if (m := re.search(r"_part(\d{3,})\.mp4$", p.name, re.IGNORECASE))}
        part_rows = db_exec(
            c, lg,
            "SELECT part_index,status FROM recording_parts WHERE session_id=%s",
            (sid,), fetchall=True,
        ) or []
        # Terminal attempts with no media are deliberately ignored.  Every
        # other DB part remains strict: a missing file still blocks a merge.
        expected_indices = {
            int(row["part_index"])
            for row in part_rows
            if row.get("part_index") is not None
            and str(row.get("status") or "").lower() != "discarded_empty"
        }
        missing_indices = sorted(expected_indices - part_indices)
        if missing_indices:
            db_update_session(c, lg, sid, ended_at="NOW()", status="merge_blocked_missing_parts",
                              parts_count=len(parts), error_message=f"누락 파트: {missing_indices}")
            lg.error(f"[{name}] 누락 파트가 있어 병합을 보류합니다: {missing_indices}")
            return
        if not ffmpeg_concat(parts, final_path, Path(c["paths"]["logs_dir"]) / f"{base}_concat.txt", lg, c):
            db_update_session(c, lg, sid, ended_at="NOW()", status="merge_failed", parts_count=len(parts), error_message="concat 실패")
            return
        size_mb = final_path.stat().st_size / (1024*1024)
        ystat = "pending" if c.get("youtube",{}).get("enabled", False) else "not_requested"
        with title_lock:
            final_broadcast_title = " / ".join(title_history) or normalize_title_text(title)
        db_insert_final(c, lg, {"streamer_name":name, "broadcast_title":final_broadcast_title, "source_url":s["url"], "quality":s["quality"], "started_at":started_at, "final_path":str(final_path), "file_size_mb":round(size_mb,2), "youtube_status":ystat, "parts_dir":str(parts_dir), "parts_count":len(parts), "session_id":sid})
        db_update_session(c, lg, sid, ended_at="NOW()", status="merged", final_path=str(final_path), parts_count=len(parts))
        lg.info(f"[{name}] 최종 병합 완료: {final_path} / YouTube: {ystat}")
    except Exception as e:
        lg.exception(f"[{name}] 세션 오류: {e}")
        db_update_session(c, lg, sid, ended_at="NOW()", status="exception", error_message=str(e), parts_dir=str(parts_dir))
    finally:
        title_monitor_stop.set()
        title_monitor_thread.join(timeout=2)
        with lock: active.pop(name, None)


def main():
    c = load_config(); ensure_dirs(c); lg = setup_logger(c)
    lg.info("="*60); lg.info(APP_NAME); lg.info("완료된 part는 즉시 Drive로 이동하고 로컬 ts를 삭제합니다."); lg.info("="*60)
    stop = {"v": False}
    def sigint(signum, frame): stop["v"] = True; lg.info("종료 요청")
    signal.signal(signal.SIGINT, sigint)
    active: Dict[str,dict] = {}; lock = threading.Lock(); threads: List[threading.Thread] = []
    threading.Thread(target=cleanup_worker, args=(c, lg, stop), daemon=True).start()
    check_interval = int(c["recording"].get("check_interval_sec", 60)); max_workers = int(c["recording"].get("max_check_workers", 5))
    while not stop["v"]:
        try:
            ss = read_streamers(c); threads = [t for t in threads if t.is_alive()]
            results=[]; lg.info(f"방송 상태 병렬 확인 시작: {len(ss)}명")
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
                for fut in concurrent.futures.as_completed([ex.submit(stream_is_live, s, lg) for s in ss]):
                    r=fut.result(); results.append(r)
                    if r.get("is_live"):
                        message = "방송 중입니다."
                    elif not r.get("check_ok", True):
                        message = "상태 조회 실패 - OFF로 처리하지 않습니다."
                    else:
                        message = "방송 중이 아닙니다."
                    lg.info(f'[{r["name"]}] {message}')
            results.sort(key=lambda r:r["name"])
            title_filter_config = load_title_filter_config(c)
            for r in results:
                if not r.get("is_live"): continue
                s = next((x for x in ss if x["name"] == r["name"]), None)
                if not s: continue
                with lock: already = s["name"] in active
                if already: lg.info(f"[{s['name']}] 이미 녹화 세션 중입니다."); continue
                title_keywords = required_title_keywords(title_filter_config, s)
                live_title = normalize_title_text(r.get("live_title"))
                if live_title and not title_is_definitive(s, live_title):
                    live_title = ""
                if title_keywords and not live_title:
                    live_title = normalize_title_text(fetch_title(s["url"], lg, quiet=True))
                    if live_title and not title_is_definitive(s, live_title):
                        live_title = ""
                if title_keywords and not title_is_allowed(title_filter_config, s, live_title):
                    required = " / ".join(title_keywords)
                    shown_title = live_title or "제목 확인 불가"
                    lg.info(
                        f"[{s['name']}] 제목 필터 대기: 필수 단어({required}) 미포함 / "
                        f"현재 제목: {shown_title}"
                    )
                    continue
                s = dict(s)
                s["_initial_live_title"] = live_title
                t = threading.Thread(target=session_worker, args=(s,c,active,lock,lg), daemon=False); t.start(); threads.append(t)
            with lock: snap = dict(active)
            live = [r["name"] for r in results if r.get("is_live")]
            lg.info("-"*60); lg.info(f"요약: 감시 {len(results)}명 / 방송중 {len(live)}명 / 녹화중 {len(snap)}명"); lg.info("-"*60)
            temp = Path(c["paths"]["temp_dir"]); merged = Path(c["paths"]["merged_dir"]); failed = Path(c["paths"]["failed_dir"])
            write_status(c, {"app":APP_NAME, "updated_at":now_text(), "streamers_count":len(ss), "active_recordings":list(snap.values()), "last_check_results":results, "paths":c["paths"], "temp_free_gb":round(free_gb(temp),2), "local_usage":{"temp_gb":round(folder_size_gb(temp),2), "merged_gb":round(folder_size_gb(merged),2), "failed_gb":round(folder_size_gb(failed),2), "recording_files":len(list(temp.glob('*.ts')))}})
            for remain in range(check_interval,0,-1):
                if stop["v"]: break
                if remain in [60,30,10] or remain <= 5: lg.info(f"다음 확인까지 {remain}초")
                time.sleep(1)
        except Exception as e:
            lg.exception(f"메인 루프 오류: {e}"); time.sleep(c["recording"].get("retry_delay_sec",15))
    for t in threads: t.join()
    lg.info("프로그램 종료")


# CHZZK_V084_DISABLE_CLEANUP_WORKER_START
def _chzzk_disabled_cleanup_worker(*args, **kwargs):
    """v0.8.4: temp를 훑는 백그라운드 cleanup worker 비활성화."""
    try:
        lg = None
        for a in args:
            if hasattr(a, 'info') and hasattr(a, 'warning'):
                lg = a
                break
        if lg:
            lg.info('v0.8.4: 백그라운드 temp cleanup worker 비활성화됨. 완료 part는 세션 루프가 직접 처리합니다.')
    except Exception:
        pass
    return None

cleanup_completed_parts_worker = _chzzk_disabled_cleanup_worker
cleanup_loop = _chzzk_disabled_cleanup_worker
cleanup_parts_worker = _chzzk_disabled_cleanup_worker
cleanup_worker = _chzzk_disabled_cleanup_worker
part_cleanup_worker = _chzzk_disabled_cleanup_worker
process_existing_parts = _chzzk_disabled_cleanup_worker
process_existing_parts_background = _chzzk_disabled_cleanup_worker
process_existing_ts_background = _chzzk_disabled_cleanup_worker
scan_completed_parts_worker = _chzzk_disabled_cleanup_worker
scan_temp_worker = _chzzk_disabled_cleanup_worker
temp_cleanup_worker = _chzzk_disabled_cleanup_worker
# CHZZK_V084_DISABLE_CLEANUP_WORKER_END

if __name__ == "__main__":
    main()

# ASYNC_PART_POSTPROCESS_PATCH_END
