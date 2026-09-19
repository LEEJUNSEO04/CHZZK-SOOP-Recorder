import json
import math
import os
import pickle
import random
import re
import shutil
import subprocess
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pymysql
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
from google_auth_oauthlib.flow import InstalledAppFlow

from youtube_thumbnail import (
    prepare_and_apply_thumbnail,
    prepare_thumbnail_variants,
    start_thumbnail_retry_thread,
)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
UPLOADER_LOG_PATH = BASE_DIR / "logs" / "youtube_uploader.log"
UPLOADER_LOG_MAX_BYTES = 20 * 1024 * 1024
_UPLOADER_LOG_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()
_QUEUE_CLAIM_LOCK = threading.Lock()
_UPLOADER_MUTEX_HANDLE = None
_PROCESSING_MISSING_LOG_AT = {}
_UPLOAD_QUOTA_LOCK = threading.Lock()
_UPLOAD_QUOTA_BLOCKED_UNTIL = 0.0
DELETED_UPLOAD_ERROR_MARKER = "YouTube uploads playlist reports Deleted video"
APP_NAME = "CHZZK YouTube Uploader v1.15 Post-Rejection 6h Fallback"
PARTIAL_FINAL_ACCEPTED = "partial final accepted: no recovery source remains"
VALIDATED_FINAL_ACCEPTED = "validated best-available final covers recovery sources"
JUNE_2026_PRIORITY_MARKER = "[QUEUE_PRIORITY:2026-06]"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]
# httplib2 uses status 0 when the connection disappears before the final
# resumable-upload response arrives.  Treat it as a transport failure so a
# 99%-complete upload resumes instead of being abandoned in DB as uploading.
RETRYABLE_HTTP_STATUS = {0, 408, 429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    socket.timeout,
    ssl.SSLError,
    OSError,
)


class UploadPausedBySchedule(Exception):
    """Raised after saving resumable upload state when upload time window is closed."""


class UploadDailyQuotaExceeded(Exception):
    """Raised when YouTube's videos.insert daily bucket is exhausted."""

    def __init__(self, wait_seconds, original_error=None):
        self.wait_seconds = max(60, int(wait_seconds or 0))
        self.original_error = original_error
        super().__init__(
            "YouTube 하루 영상 업로드 할당량 초과; "
            f"다음 갱신까지 약 {format_seconds(self.wait_seconds)} 대기"
        )


class UploadPermanentRequestError(Exception):
    """A non-retryable API request error that must not be hot-looped."""


class UploadVideoTooLongError(UploadPermanentRequestError):
    """YouTube explicitly rejected this upload because of video duration."""


def upload_schedule_config(config):
    """Return uploader schedule config.

    Default policy for this server:
    - Uploads allowed only from 01:00 to 07:00.
    - If the time window closes during an upload, pause after the current resumable chunk.
    - Saved resumable session is reused when the next allowed window starts.
    """
    yt = config.get("youtube", {}) if isinstance(config, dict) else {}
    schedule = yt.get("upload_schedule", {}) or {}
    return {
        "enabled": bool(schedule.get("enabled", yt.get("upload_schedule_enabled", True))),
        "start": str(schedule.get("start", yt.get("upload_allowed_start", "01:00"))),
        "end": str(schedule.get("end", yt.get("upload_allowed_end", "07:00"))),
        "pause_active_upload": bool(schedule.get("pause_active_upload", yt.get("pause_active_upload_outside_schedule", True))),
    }


def parse_hhmm(value, fallback):
    try:
        h, m = str(value).strip().split(":", 1)
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return fallback


def is_upload_allowed_now(config, dt=None):
    sched = upload_schedule_config(config)
    if not sched["enabled"]:
        return True
    dt = dt or datetime.now()
    start_h, start_m = parse_hhmm(sched["start"], (1, 0))
    end_h, end_m = parse_hhmm(sched["end"], (7, 0))
    now_minutes = dt.hour * 60 + dt.minute
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    if start_minutes == end_minutes:
        return True
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def seconds_until_next_allowed(config, dt=None):
    sched = upload_schedule_config(config)
    if not sched["enabled"] or is_upload_allowed_now(config, dt):
        return 0
    dt = dt or datetime.now()
    start_h, start_m = parse_hhmm(sched["start"], (1, 0))
    target = dt.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if target <= dt:
        # If start time already passed today, next start is tomorrow.
        target = target.replace(day=target.day)
        from datetime import timedelta
        target = target + timedelta(days=1)
    return max(1, int((target - dt).total_seconds()))


def format_seconds(seconds):
    seconds = int(max(0, seconds or 0))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}시간 {m}분"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg="", icon="ℹ️"):
    line = f"[{now()}] {icon} {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        # A legacy Windows console code page must never abort cleanup.
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        safe_line = line.encode(encoding, errors="backslashreplace").decode(encoding)
        try:
            print(safe_line, flush=True)
        except (BrokenPipeError, OSError, ValueError):
            # The dashboard relays this pipe. A dashboard reload must never
            # terminate an in-flight resumable upload because its reader left.
            pass
    except (BrokenPipeError, OSError, ValueError):
        # Keep uploading even when the dashboard/supervisor console vanishes.
        pass
    try:
        with _UPLOADER_LOG_LOCK:
            UPLOADER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            if UPLOADER_LOG_PATH.exists() and UPLOADER_LOG_PATH.stat().st_size >= UPLOADER_LOG_MAX_BYTES:
                rotated = UPLOADER_LOG_PATH.with_suffix(".log.1")
                rotated.unlink(missing_ok=True)
                UPLOADER_LOG_PATH.replace(rotated)
            with UPLOADER_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # Logging must never interrupt a resumable upload.
        pass


def hr():
    print("═" * 72, flush=True)


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def state_path(config):
    logs_dir = Path(config["paths"].get("logs_dir", str(BASE_DIR / "logs")))
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "youtube_uploader_state.json"


def acquire_uploader_mutex():
    """Prevent dashboard/supervisor races from starting two uploaders."""
    global _UPLOADER_MUTEX_HANDLE
    if os.name != "nt":
        return True
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\CHZZK_YOUTUBE_UPLOADER_V1")
    if not handle:
        raise OSError("YouTube uploader mutex creation failed")
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _UPLOADER_MUTEX_HANDLE = handle
    return True


def load_state(config):
    base = {
        "last_uploaded_streamer": None,
        "last_uploaded_recording_id": None,
        "last_uploaded_at": None,
        "current_upload": None,
        "current_uploads": {},
        "resumable_sessions": {},
        "history": [],
    }
    with _STATE_LOCK:
        p = state_path(config)
        if not p.exists():
            return base
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in base.items():
                data.setdefault(k, v)
            return data
        except Exception:
            return base


def save_state(config, state):
    with _STATE_LOCK:
        path = state_path(config)
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        payload = json.dumps(state, ensure_ascii=False, indent=2)
        temp.write_text(payload, encoding="utf-8")
        last_error = None
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.05 * (attempt + 1))
        temp.unlink(missing_ok=True)
        raise last_error


def get_file_size(path):
    try:
        return Path(path).stat().st_size
    except Exception:
        return None


def remove_empty_june_month_dir(file_path):
    """Remove only an empty legacy 2026-06 month directory.

    This deliberately uses ``rmdir`` rather than recursive deletion.  A month
    directory that still contains even one queued/recovery file is preserved.
    """
    try:
        parent = Path(file_path).resolve().parent
        if parent.name != "2026-06":
            return False
        staging = parent / "_merge_staging"
        if staging.is_dir():
            try:
                staging.rmdir()
            except OSError:
                pass
        parent.rmdir()
        log(f"비어진 2026-06 날짜 폴더 정리: {parent}", "🧹")
        return True
    except (OSError, ValueError):
        return False


def find_ffmpeg(config):
    """Return ffmpeg path. Prefer config/youtube.ffmpeg_path, then local tools, then PATH."""
    yt = config.get("youtube", {}) if isinstance(config, dict) else {}
    candidates = [
        yt.get("ffmpeg_path"),
        str(BASE_DIR / "tools" / "ffmpeg.exe"),
        str(BASE_DIR / "ffmpeg.exe"),
        "ffmpeg",
    ]
    for c in candidates:
        if not c:
            continue
        p = Path(c)
        if c == "ffmpeg" or p.exists():
            return c
    return "ffmpeg"


def parse_duration_from_ffmpeg_output(text):
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text or "")
    if not m:
        return None
    h, mnt, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mnt * 60 + sec


def format_duration(seconds):
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def get_video_duration_sec(config, file_path):
    """Use ffmpeg output to read duration. Works even when ffprobe is not installed."""
    ffmpeg = find_ffmpeg(config)
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        return parse_duration_from_ffmpeg_output((proc.stdout or "") + "\n" + (proc.stderr or ""))
    except Exception as e:
        log(f"영상 길이 확인 실패: {e}", "⚠️")
        return None


def table_columns(conn, table_name="recordings"):
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {table_name}")
        return {r["Field"] for r in cur.fetchall()}


def record_exists_by_final_path(conn, final_path):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, youtube_status FROM recordings WHERE final_path=%s LIMIT 1",
            (str(final_path),),
        )
        return cur.fetchone()


def insert_recording_dynamic(conn, data):
    cols = table_columns(conn, "recordings")
    filtered = {k: v for k, v in data.items() if k in cols}
    if not filtered:
        raise RuntimeError("insert_recording_dynamic: no matching columns")
    names = list(filtered.keys())
    placeholders = ", ".join(["%s"] * len(names))
    sql = f"INSERT INTO recordings ({', '.join(names)}) VALUES ({placeholders})"
    with conn.cursor() as cur:
        cur.execute(sql, [filtered[k] for k in names])
        return cur.lastrowid


def validate_split_output_durations(duration_sec, segment_sec, part_durations):
    """Return an error when ffmpeg did not create real bounded segments."""
    try:
        duration_sec, segment_sec = float(duration_sec), float(segment_sec)
        if not all(math.isfinite(v) and v > 0 for v in (duration_sec, segment_sec)):
            return "invalid source duration or segment duration"
        part_durations = [float(value) for value in part_durations]
    except (TypeError, ValueError, OverflowError):
        return "one or more split durations could not be read"
    if not part_durations or any(not math.isfinite(v) or v <= 0 for v in part_durations):
        return "one or more split durations could not be read"
    duration_tolerance = max(120.0, float(duration_sec) * 0.03)
    part_limit_tolerance = max(120.0, float(segment_sec) * 0.02)
    if duration_sec > segment_sec + part_limit_tolerance and len(part_durations) < 2:
        return (
            f"expected multiple parts for {format_duration(duration_sec)}, "
            f"but ffmpeg produced {len(part_durations)}"
        )
    if any(value is None or value <= 0 for value in part_durations):
        return "one or more split durations could not be read"
    if any(float(value) > segment_sec + part_limit_tolerance for value in part_durations):
        longest = max(float(value) for value in part_durations)
        return (
            f"split part exceeds limit: {format_duration(longest)} > "
            f"{format_duration(segment_sec + part_limit_tolerance)}"
        )
    split_duration_total = sum(float(value) for value in part_durations)
    if abs(split_duration_total - float(duration_sec)) > duration_tolerance:
        return (
            f"split duration coverage mismatch: source={format_duration(duration_sec)}, "
            f"children={format_duration(split_duration_total)}, "
            f"tolerance={format_duration(duration_tolerance)}"
        )
    return ""


def split_too_long_video(conn, config, row, duration_sec, trigger_reason=None):
    """Split only after a confirmed YouTube limit requires a smaller file."""
    yt = config.get("youtube", {})
    rec_id = row.get("id")
    file_path = Path(row.get("final_path") or "")
    trigger_reason = trigger_reason or "YouTube가 원본 영상 길이를 실제로 거절함"
    if not file_path.exists():
        update_record(conn, rec_id, youtube_status="file_missing", youtube_error=f"too-long original missing before split: {file_path}")
        log(f"분할 원본 없음 #{rec_id}: {file_path}", "❌")
        return 0

    ffmpeg = find_ffmpeg(config)
    segment_sec = int(yt.get("split_segment_seconds", 6 * 3600))
    split_root = Path(yt.get("split_output_dir") or (file_path.parent / "_split_upload"))
    out_dir = split_root / f"{file_path.stem}_split_{rec_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Never overwrite an earlier attempt: a worker may already be reading it.
    if any(out_dir.iterdir()):
        update_record(conn, rec_id, youtube_status="failed", youtube_error=(
            f"split output already exists; preserved for manual review: {out_dir}"
        )[:1000])
        return 0
    out_pattern = out_dir / f"{file_path.stem}_part%02d.mp4"

    log(
        f"{trigger_reason} / fallback 분할 시작 #{rec_id}: "
        f"{format_duration(duration_sec)} → {format_duration(segment_sec)} 단위",
        "✂️",
    )
    log(f"분할 저장 폴더: {out_dir}", "📁")

    cmd = [
        ffmpeg,
        "-hide_banner", "-nostdin", "-n",
        "-fflags", "+genpts+discardcorrupt",
        "-err_detect", "ignore_err",
        "-i", str(file_path),
        "-map", "0:v:0", "-map", "0:a?",
        "-dn", "-sn",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_sec),
        "-reset_timestamps", "1",
        "-segment_format", "mp4",
        str(out_pattern),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(BASE_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    last_line_time = 0
    if proc.stdout:
        for raw in proc.stdout:
            text = raw.strip()
            if not text:
                continue
            # ffmpeg can be very chatty; keep progress readable.
            if "time=" in text or "frame=" in text or "error" in text.lower() or "failed" in text.lower():
                now_t = time.time()
                if now_t - last_line_time > 15 or "error" in text.lower() or "failed" in text.lower():
                    log(f"split: {text[:220]}", "✂️")
                    last_line_time = now_t
    code = proc.wait()
    # Splitting can take long enough for MySQL to close the idle connection.
    # Reconnect before registering children or changing the parent state.
    conn.ping(reconnect=True)
    if code != 0:
        update_record(conn, rec_id, youtube_status="split_required", youtube_error=f"ffmpeg split failed with code {code}")
        log(f"분할 실패 #{rec_id}: ffmpeg code={code}", "❌")
        return 0

    parts = sorted(out_dir.glob("*.mp4"))
    # Include short tail files too; silently dropping them loses final seconds.
    parts = [p for p in parts if p.is_file()]
    if not parts:
        update_record(conn, rec_id, youtube_status="split_required", youtube_error="split finished but no valid output files")
        log(f"분할 결과 파일 없음 #{rec_id}", "❌")
        return 0

    # A segment-muxer exit code of zero is not enough.  Corrupt or keyframe-
    # starved inputs can produce one full-length copy instead of the requested
    # six-hour parts.  Registering that copy would simply upload the same bad
    # video again.  Require multiple bounded parts and duration coverage before
    # any child DB rows are created.
    part_durations = [get_video_duration_sec(config, part) for part in parts]
    split_validation_error = validate_split_output_durations(
        duration_sec,
        segment_sec,
        part_durations,
    )
    original_size = file_path.stat().st_size
    child_total = sum(p.stat().st_size for p in parts)
    size_delta = abs(child_total - original_size)
    size_tolerance = max(64 * 1024 * 1024, int(original_size * 0.02))
    if not split_validation_error and (
        any(p.stat().st_size == 0 for p in parts) or size_delta > size_tolerance
    ):
        split_validation_error = (
            f"split size coverage mismatch: delta={size_delta}, tolerance={size_tolerance}"
        )

    if split_validation_error:
        update_record(
            conn,
            rec_id,
            youtube_status="failed",
            youtube_error=(
                f"fallback split validation failed: {split_validation_error}; "
                f"original and generated files preserved for manual review: {out_dir}"
            )[:1000],
        )
        log(
            f"분할 검증 실패 #{rec_id}: {split_validation_error}; "
            "DB 자식 등록 없이 원본/결과 보존",
            "🛡️",
        )
        return 0

    inserted = 0
    registered = 0
    for idx, part in enumerate(parts, 1):
        existing = record_exists_by_final_path(conn, str(part))
        if existing:
            safe_existing_statuses = {
                "pending", "uploading", "uploaded_pending_youtube_processing",
                "processing", "done",
            }
            if existing.get("youtube_status") in safe_existing_statuses:
                registered += 1
                log(f"이미 등록된 분할본 건너뜀: {part.name}", "↩️")
            else:
                log(
                    f"분할본 DB 상태가 안전하지 않아 원본 보존: {part.name} / "
                    f"{existing.get('youtube_status')}",
                    "⚠️",
                )
            continue
        size_mb = part.stat().st_size / (1024 ** 2)
        sid_base = row.get("session_id") or f"recording_{rec_id}"
        inherited_priority = (
            f" {JUNE_2026_PRIORITY_MARKER};"
            if JUNE_2026_PRIORITY_MARKER in str(row.get("youtube_error") or "")
            else ""
        )
        data = {
            "streamer_name": row.get("streamer_name"),
            "broadcast_title": row.get("broadcast_title"),
            "status": "done",
            "youtube_status": "pending",
            "final_path": str(part),
            "file_size_mb": size_mb,
            "parts_count": 1,
            "parts_dir": str(out_dir),
            "session_id": f"{sid_base}_yt_split_{idx:02d}",
            "started_at": row.get("started_at"),
            "youtube_error": (
                f"Split from recording id {rec_id}; queue anchored;{inherited_priority} "
                f"source reason: {trigger_reason}; original duration {format_duration(duration_sec)}"
            ),
        }
        new_id = insert_recording_dynamic(conn, data)
        inserted += 1
        registered += 1
        log(f"분할본 pending 등록 #{new_id}: {part.name} / {size_mb/1024:.2f}GB", "✅")

    # Keep the rejected original until every split child has completed YouTube
    # processing. Registering the children proves that the split files exist,
    # but it does not prove that YouTube accepted them. A separate, explicit
    # parent cleanup can retire the original after all children are verified.
    all_registered = registered == len(parts)
    if all_registered and size_delta <= size_tolerance:
        update_record_fresh(
            config,
            rec_id,
            youtube_status="split_parent_ready",
            youtube_error=(
                f"{trigger_reason} ({format_duration(duration_sec)}); "
                f"verified {registered} split files ({inserted} newly registered) at {out_dir}; "
                "original preserved until every split child finishes YouTube processing"
            ),
        )
        log(
            f"분할본 {registered}개 검증/등록 완료, YouTube 처리 완료 전 원본 보존: "
            f"{file_path.name}",
            "🛡️",
        )
    else:
        update_record_fresh(
            config,
            rec_id,
            youtube_status="split_required",
            youtube_error=(
                f"{trigger_reason} ({format_duration(duration_sec)}); "
                f"split verification incomplete: registered={registered}/{len(parts)}, "
                f"size_delta={size_delta}, tolerance={size_tolerance}; output={out_dir}"
            ),
        )
    return registered


def preflight_upload_safety(conn, config, row):
    """Return True when a local file is safe to upload.

    Long videos are intentionally sent intact first.  YouTube's documented
    12-hour limit is not applied consistently to every channel, so pre-splitting
    wastes time and duplicate disk space on channels that accept full-length
    VODs.  A confirmed YouTube length rejection is handled later by the
    fallback splitter.
    """
    yt = config.get("youtube", {})
    rec_id = row.get("id")
    file_path = Path(row.get("final_path") or "")
    if not file_path.exists():
        update_record(conn, rec_id, youtube_status="file_missing", youtube_error=f"file not found: {file_path}")
        log(f"파일 없음 #{rec_id}: {file_path}", "❌")
        return False

    if not upload_session_is_complete(conn, row):
        return False

    size_gb = file_path.stat().st_size / (1024 ** 3)
    parts_count = int(row.get("parts_count") or 0)
    small_guard_gb = float(yt.get("small_file_guard_gb", 4.0))
    small_guard_parts = int(yt.get("small_file_guard_parts", 8))
    if size_gb < small_guard_gb and parts_count >= small_guard_parts:
        # DB parts_count can remain high after the actual part files were cleaned.
        # Hold a suspiciously small final only when a real remerge source exists;
        # otherwise upload the last playable copy and let normal cleanup reclaim it.
        recovery_candidates = []
        raw_parts_dir = row.get("parts_dir")
        if raw_parts_dir:
            recovery_candidates.append(Path(raw_parts_dir))

        session_id = str(row.get("session_id") or file_path.stem)
        drive_dir = Path(config.get("paths", {}).get("drive_dir") or file_path.parent)
        staging_root = drive_dir.parent / "_merge_staging"
        recovery_candidates.extend([
            staging_root / session_id,
            staging_root / f"{session_id}_stitched",
        ])

        recoverable = False
        for candidate in recovery_candidates:
            try:
                if candidate.exists() and any(
                    p.is_file()
                    and p.stat().st_size > 0
                    and p.suffix.lower() in {".mp4", ".ts"}
                    for p in candidate.rglob("*")
                ):
                    recoverable = True
                    break
            except OSError:
                continue

        msg = f"Suspicious small final before upload: final {size_gb:.2f}GB / parts {parts_count}"
        if recoverable:
            update_record(
                conn,
                rec_id,
                youtube_status="not_requested",
                youtube_error=f"{msg}; recoverable parts/staging exists, waiting for remerge",
            )
            log(f"업로드 차단 #{rec_id}: {msg}; 복구 파트가 남아 재병합 대기", "🛑")
            return False

        log(f"부분본 업로드 허용 #{rec_id}: {msg}; 복구 파트 없음", "⚠️")

    duration_sec = get_video_duration_sec(config, file_path)
    max_sec = int(yt.get("max_video_duration_sec", 12 * 3600))
    if duration_sec is not None:
        log(f"영상 길이 확인 #{rec_id}: {format_duration(duration_sec)}", "⏱️")
        if not upload_duration_is_complete(conn, row, duration_sec):
            return False
    else:
        log(f"영상 길이를 확인하지 못했습니다 #{rec_id}. 업로드는 진행하되 주의 필요", "⚠️")
        return True

    # YouTube's file-size ceiling is independent of duration.  Normal long
    # videos stay intact, but a file above this hard ceiling cannot succeed as
    # a single upload and therefore must use the verified split path.
    max_size_gb = float(yt.get("max_video_size_gb", 256.0))
    if size_gb > max_size_gb:
        reason = f"YouTube 파일 크기 한도 초과: {size_gb:.2f}GB > {max_size_gb:.0f}GB"
        update_record(conn, rec_id, youtube_status="split_required", youtube_error=reason)
        log(f"업로드 전 분할 필요 #{rec_id}: {reason}", "🛑")
        if bool(yt.get("auto_split_too_long", True)):
            try:
                split_too_long_video(
                    conn,
                    config,
                    row,
                    duration_sec,
                    trigger_reason=reason,
                )
            except Exception as e:
                update_record(
                    conn,
                    rec_id,
                    youtube_status="split_required",
                    youtube_error=f"{reason}; split error: {e}",
                )
                log(f"파일 크기 fallback 분할 오류 #{rec_id}: {e}", "❌")
        return False

    if duration_sec > max_sec:
        if bool(yt.get("try_full_upload_over_12h", True)):
            log(
                f"장시간 원본 우선 업로드 허용 #{rec_id}: "
                f"{format_duration(duration_sec)} / YouTube 실제 거절 시에만 분할",
                "🛡️",
            )
        else:
            msg = f"Duration {format_duration(duration_sec)} exceeds configured pre-split limit"
            update_record(conn, rec_id, youtube_status="split_required", youtube_error=msg)
            log(f"업로드 차단 #{rec_id}: {msg}", "🛑")
            if bool(yt.get("auto_split_too_long", True)):
                try:
                    split_too_long_video(conn, config, row, duration_sec)
                except Exception as e:
                    update_record(conn, rec_id, youtube_status="split_required", youtube_error=f"{msg}; split error: {e}")
                    log(f"자동 분할 오류 #{rec_id}: {e}", "❌")
            return False
    return True


def _is_split_child(row):
    path_text = str(row.get("final_path") or "").lower()
    error_text = str(row.get("youtube_error") or "").lower()
    return "split_upload" in path_text or error_text.startswith("split from recording")


def _partial_final_was_accepted(row):
    error_text = str(row.get("youtube_error") or "").lower()
    return PARTIAL_FINAL_ACCEPTED in error_text or VALIDATED_FINAL_ACCEPTED in error_text


def recovery_sources_exist(config, row):
    """Treat unreadable sources conservatively; include SOOP raw audio/video."""
    session_id = str(row.get("session_id") or Path(row.get("final_path") or "").stem)
    final_path = Path(row.get("final_path") or "")
    candidates = []
    raw_parts = str(row.get("parts_dir") or "")
    if raw_parts and not raw_parts.upper().startswith("GDRIVE_PRESERVED:"):
        candidates.append(Path(raw_parts))
    if final_path.parent:
        candidates.extend([
            final_path.parent / f"{final_path.stem}_parts",
            final_path.parent / f"{final_path.stem}_stitched_parts",
            final_path.parent / f"{final_path.stem}_orphan_parts",
        ])
    drive_dir = Path(config.get("paths", {}).get("drive_dir") or final_path.parent)
    staging = drive_dir.parent / "_merge_staging"
    candidates.extend([staging / session_id, staging / f"{session_id}_stitched"])
    for candidate in candidates:
        try:
            if candidate.is_dir() and any(
                p.is_file() and p.stat().st_size > 0 and p.suffix.lower() in {".ts", ".mp4", ".h264", ".aac"}
                for p in candidate.rglob("*")
            ):
                return True
        except OSError:
            return True
    temp_dir = Path(config.get("paths", {}).get("temp_dir") or BASE_DIR / "temp")
    failed_dir = Path(config.get("paths", {}).get("failed_dir") or BASE_DIR / "failed")
    try:
        safe_prefix = session_id.replace(":", "-")
        if any(
            p.is_file() and p.stat().st_size > 0
            and p.suffix.lower() in {".ts", ".mp4", ".h264", ".aac"}
            for source_dir in (temp_dir, failed_dir, failed_dir / "recovered_source_backup")
            for p in source_dir.glob(f"{safe_prefix}*")
        ):
            return True
    except OSError:
        return True
    return False


def release_not_ready_without_sources(conn, config):
    """Keep best-available finals in the queue when no repair source exists.

    This also stamps still-pending rows *before* a worker claims them.  Without
    that pre-claim stamp, a stale failed session is briefly changed from
    ``pending`` to ``uploading`` and then dropped to ``not_ready`` on every
    retry, which is the disappearing-queue bug seen on the dashboard.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM recordings
            WHERE youtube_status IN ('pending','not_ready')
              AND status='done'
              AND final_path IS NOT NULL
            ORDER BY id
            """
        )
        rows = cur.fetchall()
    released = 0
    for row in rows:
        reason = str(row.get("youtube_error") or "")
        # This routine runs on every uploader scan.  Once a best-available
        # final has already been accepted, stale failed/missing part rows may
        # still exist in the session tables.  Re-evaluating those rows used to
        # rewrite the same marker and print a fake "recovered" line forever.
        # The accepted marker is durable, so an accepted row is already done.
        if _partial_final_was_accepted(row):
            continue
        held_reason = (
            reason.startswith("session/part completeness hold:")
            or reason.startswith("session has ")
            or reason.startswith("recording session is not complete:")
            or reason.startswith("possible one-hour Google Drive segment fragment")
        )
        if row.get("youtube_status") == "pending" and not held_reason:
            session_id = str(row.get("session_id") or "")
            if not session_id:
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM recording_sessions WHERE session_id=%s LIMIT 1",
                        (session_id,),
                    )
                    session = cur.fetchone() or {}
                    cur.execute(
                        """
                        SELECT COUNT(*) AS unsafe_count
                        FROM recording_parts
                        WHERE session_id=%s
                          AND status IN (
                            'recording','processing','ffmpeg_failed',
                            'drive_verify_failed','missing_ts'
                          )
                        """,
                        (session_id,),
                    )
                    unsafe_count = int((cur.fetchone() or {}).get("unsafe_count") or 0)
                session_status = str(session.get("status") or "").lower()
                held_reason = unsafe_count > 0 or session_status in {
                    "recording", "processing", "merging", "no_parts",
                    "postprocess_timeout", "merge_failed",
                    "merge_blocked_missing_parts", "exception",
                }
                if held_reason:
                    reason = (
                        f"pre-claim session hold: status={session_status or '-'}, "
                        f"unsafe_parts={unsafe_count}"
                    )
            except Exception:
                continue
        if not held_reason:
            continue
        # A stale final must never override an active capture/merge. Recheck
        # even rows whose error text already describes a completeness hold.
        session_id = str(row.get("session_id") or "")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM recording_sessions WHERE session_id=%s LIMIT 1",
                (session_id,),
            )
            session = cur.fetchone() or {}
            cur.execute(
                "SELECT COUNT(*) AS active_count FROM recording_parts "
                "WHERE session_id=%s AND status IN ('recording','processing')",
                (session_id,),
            )
            active_count = int((cur.fetchone() or {}).get("active_count") or 0)
        if str(session.get("status") or "").lower() in {"recording", "processing", "merging"} or active_count:
            continue
        file_path = Path(row.get("final_path") or "")
        try:
            file_stat = file_path.stat()
            if not file_path.is_file() or file_stat.st_size < 50 * 1024 * 1024:
                continue
            if time.time() - file_path.stat().st_mtime < 2 * 3600:
                continue
        except OSError:
            continue
        if recovery_sources_exist(config, row):
            continue
        duration = get_video_duration_sec(config, file_path)
        if duration is None or not math.isfinite(duration) or duration < 30:
            continue
        after_stat = file_path.stat()
        if (after_stat.st_size, after_stat.st_mtime_ns) != (file_stat.st_size, file_stat.st_mtime_ns):
            continue
        marker = f"{PARTIAL_FINAL_ACCEPTED}; duration={format_duration(duration)}; previous={reason}"[:1000]
        # Compare-and-set: scanning/probing may overlap a worker claim or a
        # user's hold. Do not reset an uploading/changed row back to pending.
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE recordings SET youtube_status='pending', youtube_error=%s
                WHERE id=%s AND status='done' AND youtube_status=%s
                  AND youtube_error <=> %s AND final_path=%s
                  AND NOT EXISTS (SELECT 1 FROM recording_sessions s
                    WHERE s.session_id=recordings.session_id
                      AND s.status IN ('recording','processing','merging'))
                  AND NOT EXISTS (SELECT 1 FROM recording_parts p
                    WHERE p.session_id=recordings.session_id
                      AND p.status IN ('recording','processing'))""",
                (marker, row.get("id"), row.get("youtube_status"),
                 row.get("youtube_error"), row.get("final_path")),
            )
            if cur.rowcount != 1:
                continue
        log(
            f"복구 원본 없음 → 길이 확인된 최종본 대기열 유지/복귀 "
            f"#{row.get('id')}: {file_path.name} / {format_duration(duration)}",
            "✅",
        )
        released += 1
    return released


def upload_session_is_complete(conn, row):
    """Block source fragments until recorder/merge DB state proves completion."""
    if _is_split_child(row) or _partial_final_was_accepted(row):
        return True

    rec_id = row.get("id")
    session_id = str(row.get("session_id") or "")
    if not session_id or session_id.startswith("gdrive_recovered"):
        return True

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, final_path FROM recording_sessions WHERE session_id=%s LIMIT 1",
                (session_id,),
            )
            session = cur.fetchone() or {}
            cur.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done_count,
                       SUM(CASE WHEN status IN ('recording','processing','ffmpeg_failed','drive_verify_failed','missing_ts') THEN 1 ELSE 0 END) AS unsafe_count,
                       MAX(part_index) AS max_part_index
                FROM recording_parts
                WHERE session_id=%s
                """,
                (session_id,),
            )
            parts = cur.fetchone() or {}
    except Exception as exc:
        update_record(conn, rec_id, youtube_status="not_ready", youtube_error=f"session completeness check failed: {exc}")
        log(f"업로드 보류 #{rec_id}: 세션 완료 상태 확인 실패", "🛡️")
        return False

    status = str(session.get("status") or "").lower()
    db_final = str(session.get("final_path") or "")
    row_final = str(row.get("final_path") or "")
    if status == "stitched" and db_final:
        try:
            same_final = Path(db_final).resolve() == Path(row_final).resolve()
        except OSError:
            same_final = db_final.lower() == row_final.lower()
        if not same_final:
            update_record(
                conn, rec_id, youtube_status="not_requested",
                youtube_error=f"source session already stitched into: {db_final}",
            )
            log(f"중복 source 업로드 제외 #{rec_id}: stitched 최종본에 이미 포함", "🧩")
            return False

    unsafe_count = int(parts.get("unsafe_count") or 0)
    # Terminal merged/stopped sessions can retain historical ffmpeg_failed
    # bookkeeping rows after the final was successfully published.  Do not
    # freeze the entire historical queue for that stale metadata; active and
    # incomplete session states remain blocked below.
    if unsafe_count and status not in {"merged", "stitched", "stopped"}:
        update_record(
            conn, rec_id, youtube_status="not_ready",
            youtube_error=f"session has {unsafe_count} unfinished/failed recording parts",
        )
        log(f"업로드 보류 #{rec_id}: 미완료/실패 part {unsafe_count}개", "🛡️")
        return False

    if status in {
        "recording", "processing", "merging", "no_parts", "postprocess_timeout",
        "merge_failed", "merge_blocked_missing_parts", "exception",
    }:
        update_record(
            conn, rec_id, youtube_status="not_ready",
            youtube_error=f"recording session is not complete: {status}",
        )
        log(f"업로드 보류 #{rec_id}: 세션 상태 {status}", "🛡️")
        return False

    return True


def upload_duration_is_complete(conn, row, duration_sec):
    """Catch exact segment-duration files that lack trustworthy completion evidence."""
    if _is_split_child(row) or _partial_final_was_accepted(row):
        return True
    # Recorder segments are one hour.  A final landing in this narrow window
    # is suspicious only when it came from recovery or claims multiple parts.
    if not (55 * 60 <= float(duration_sec) <= 65 * 60):
        return True
    parts_count = int(row.get("parts_count") or 0)
    parts_dir = str(row.get("parts_dir") or "")
    error_text = str(row.get("youtube_error") or "")
    recovered = parts_dir.upper().startswith("GDRIVE_PRESERVED:") or "recovered from" in error_text.lower()
    if not recovered and parts_count <= 1:
        return True
    rec_id = row.get("id")
    update_record(
        conn, rec_id, youtube_status="not_ready",
        youtube_error=(
            f"possible one-hour segment fragment: duration {format_duration(duration_sec)}, "
            f"parts_count {parts_count}; manual/remerge audit required"
        ),
    )
    log(f"업로드 보류 #{rec_id}: 정확히 1시간 부근인 분할 조각 의심", "🛡️")
    return False


def session_key(row):
    return str(row.get("id"))


def set_current_upload(config, row, progress=0, worker_id=None):
    with _STATE_LOCK:
        state = load_state(config)
        rec_id = str(row.get("id"))
        current = {
            "id": row.get("id"),
            "worker_id": worker_id,
            "streamer_name": row.get("streamer_name"),
            "final_path": row.get("final_path"),
            "file_size": get_file_size(row.get("final_path")),
            "started_at": now(),
            "progress": progress,
        }
        state.setdefault("current_uploads", {})[rec_id] = current
        # Preserve the legacy field for older dashboards during a rolling update.
        state["current_upload"] = current
        save_state(config, state)


def update_current_progress(config, row, progress):
    with _STATE_LOCK:
        state = load_state(config)
        rec_id = str(row.get("id"))
        uploads = state.setdefault("current_uploads", {})
        current = uploads.get(rec_id)
        if not current:
            current = {
                "id": row.get("id"),
                "streamer_name": row.get("streamer_name"),
                "final_path": row.get("final_path"),
                "file_size": get_file_size(row.get("final_path")),
                "started_at": now(),
            }
            uploads[rec_id] = current
        current["progress"] = progress
        current["updated_at"] = now()
        state["current_upload"] = current
        save_state(config, state)


def clear_current_upload(config, row=None):
    with _STATE_LOCK:
        state = load_state(config)
        uploads = state.setdefault("current_uploads", {})
        if row is None:
            uploads.clear()
        else:
            uploads.pop(str(row.get("id")), None)
        remaining = list(uploads.values())
        state["current_upload"] = remaining[0] if remaining else None
        save_state(config, state)


def save_resumable_session(config, row, uri, progress_bytes=0, progress_percent=0):
    if not uri:
        return
    with _STATE_LOCK:
        state = load_state(config)
        sessions = state.setdefault("resumable_sessions", {})
        sessions[session_key(row)] = {
            "recording_id": row.get("id"),
            "streamer_name": row.get("streamer_name"),
            "final_path": row.get("final_path"),
            "file_size": get_file_size(row.get("final_path")),
            "resumable_uri": uri,
            "progress_bytes": int(progress_bytes or 0),
            "progress_percent": int(progress_percent or 0),
            "updated_at": now(),
        }
        save_state(config, state)


def get_resumable_session(config, row):
    state = load_state(config)
    info = (state.get("resumable_sessions") or {}).get(session_key(row))
    if not info:
        return None
    path = row.get("final_path")
    size = get_file_size(path)
    if info.get("final_path") != path:
        return None
    if size is None or info.get("file_size") != size:
        return None
    if not info.get("resumable_uri"):
        return None
    return info


def clear_resumable_session(config, row):
    with _STATE_LOCK:
        state = load_state(config)
        sessions = state.setdefault("resumable_sessions", {})
        sessions.pop(session_key(row), None)
        save_state(config, state)


def remember_uploaded(config, row):
    with _STATE_LOCK:
        state = load_state(config)
        streamer = row.get("streamer_name") or "unknown"
        state["last_uploaded_streamer"] = streamer
        state["last_uploaded_recording_id"] = row.get("id")
        state["last_uploaded_at"] = now()
        current = state.get("current_upload") or {}
        # The processing-cleanup thread often finishes an older video while a
        # newer one is uploading.  Never erase that unrelated active upload.
        if str(current.get("id") or "") == str(row.get("id") or ""):
            state["current_upload"] = None
        uploads = state.setdefault("current_uploads", {})
        uploads.pop(str(row.get("id")), None)
        if not state.get("current_upload") and uploads:
            state["current_upload"] = next(iter(uploads.values()))
        state.setdefault("resumable_sessions", {}).pop(session_key(row), None)
        history = state.get("history") or []
        history.append({"id": row.get("id"), "streamer_name": streamer, "uploaded_at": now()})
        state["history"] = history[-30:]
        save_state(config, state)


def db_connect(config):
    db = config["database"]
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


def get_credentials(config):
    yt = config.get("youtube", {})
    token_pickle = Path(yt.get("token_pickle_path", str(BASE_DIR / "token.pickle")))
    client_secret = Path(yt.get("client_secret_path", str(BASE_DIR / "client_secret.json")))
    creds = None
    if token_pickle.exists():
        with token_pickle.open("rb") as f:
            creds = pickle.load(f)
    if not creds or not getattr(creds, "valid", False):
        if creds and getattr(creds, "expired", False) and getattr(creds, "refresh_token", None):
            log("YouTube 인증 토큰 갱신 중...", "🔐")
            creds.refresh(Request())
        else:
            if not client_secret.exists():
                raise FileNotFoundError(f"client_secret.json 없음: {client_secret}")
            log("YouTube 최초 인증이 필요합니다. 브라우저에서 로그인하세요.", "🔐")
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
            creds = flow.run_local_server(port=0)
        with token_pickle.open("wb") as f:
            pickle.dump(creds, f)
    return creds


def youtube_service(config):
    return build("youtube", "v3", credentials=get_credentials(config))


def update_record(conn, rec_id, **kwargs):
    fields, values = [], []
    for key, value in kwargs.items():
        if value == "NOW()":
            fields.append(f"{key}=NOW()")
        else:
            fields.append(f"{key}=%s")
            values.append(value)
    if not fields:
        return
    values.append(rec_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE recordings SET {', '.join(fields)} WHERE id=%s", values)


def update_record_fresh(config, rec_id, **kwargs):
    """Update through a new DB connection after a potentially long upload."""
    last_error = None
    for attempt in range(3):
        try:
            with db_connect(config) as fresh_conn:
                update_record(fresh_conn, rec_id, **kwargs)
            return
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"fresh DB update failed for recording {rec_id}: {last_error}")


def recover_stale_uploading(conn, config):
    if not config.get("youtube", {}).get("recover_uploading_on_start", True):
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recordings
            SET youtube_status='pending',
                youtube_error='Recovered from stale uploading state'
            WHERE youtube_status='uploading'
              AND (youtube_video_id IS NULL OR youtube_video_id='')
              AND status='done'
              AND final_path IS NOT NULL
            """
        )
        return cur.rowcount


def recover_upload_limit_failures(conn):
    """Requeue rows incorrectly isolated by YouTube's rolling upload cap."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE recordings
            SET youtube_status='pending',
                youtube_error='Recovered uploadLimitExceeded; waiting for next rolling 24-hour upload slot'
            WHERE youtube_status='failed'
              AND youtube_error LIKE '%uploadLimitExceeded%'
              AND status='done'
              AND final_path IS NOT NULL
              AND (youtube_video_id IS NULL OR youtube_video_id='')
            """
        )
        return cur.rowcount


def get_queue_counts(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
              SUM(CASE WHEN youtube_status='pending' THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN youtube_status='uploading' THEN 1 ELSE 0 END) AS uploading,
              SUM(CASE WHEN youtube_status='done' THEN 1 ELSE 0 END) AS done,
              SUM(CASE WHEN youtube_status='not_ready' THEN 1 ELSE 0 END) AS not_ready,
              SUM(CASE WHEN youtube_status IN ('failed','file_missing') THEN 1 ELSE 0 END) AS failed
            FROM recordings
            """
        )
        row = cur.fetchone() or {}
    return {k: int(row.get(k) or 0) for k in ["pending", "uploading", "done", "not_ready", "failed"]}


def get_row_by_id(conn, rec_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM recordings
            WHERE id=%s
              AND status='done'
              AND final_path IS NOT NULL
              AND youtube_status IN ('pending','uploading')
            LIMIT 1
            """,
            (rec_id,),
        )
        return cur.fetchone()


def split_queue_priority_sql():
    """Return the stable queue key used by both originals and split children.

    Split children receive new auto-increment IDs, but their ``youtube_error``
    keeps the source recording ID.  Ordering by that source ID preserves the
    parent's original place instead of sending newly-created parts to the end.
    """
    return """
        CASE
            WHEN LOCATE('[QUEUE_PRIORITY:2026-06]', COALESCE(youtube_error, '')) > 0
            THEN 0
            WHEN youtube_error REGEXP '^Split from recording id [0-9]+; queue anchored;'
            THEN CAST(
                SUBSTRING_INDEX(SUBSTRING_INDEX(youtube_error, ';', 1), ' ', -1)
                AS UNSIGNED
            )
            ELSE id
        END
    """


def select_next_upload(conn, config):
    state = load_state(config)
    resume_ids = []
    # Resume real, partially transferred sessions before merely claimed/current
    # rows.  A process restart must not send a 47% upload to the back of the
    # queue while starting two brand-new 0% uploads.
    resumable_items = list((state.get("resumable_sessions") or {}).items())
    resumable_items.sort(
        key=lambda item: (
            -int((item[1] or {}).get("progress_percent") or 0),
            str((item[1] or {}).get("updated_at") or ""),
        )
    )
    resume_ids.extend(item_id for item_id, _info in resumable_items)
    for item in (state.get("current_uploads") or {}).values():
        if item.get("id") is not None:
            resume_ids.append(item.get("id"))
    current = state.get("current_upload") or {}
    if current.get("id") is not None:
        resume_ids.append(current.get("id"))
    seen_resume_ids = set()
    for current_id in resume_ids:
        if str(current_id) in seen_resume_ids:
            continue
        seen_resume_ids.add(str(current_id))
        row = get_row_by_id(conn, current_id)
        if row and row.get("final_path") and Path(row["final_path"]).exists():
            if row.get("youtube_status") == "pending":
                return row, "resume saved upload"

    last_streamer = state.get("last_uploaded_streamer")
    queue_priority = split_queue_priority_sql()
    # Do not claim a row whose recorder session still needs repair. Claiming it
    # first and discovering the problem in preflight made PENDING items appear
    # to vanish from the queue. A durable accepted marker is the only bypass
    # for a playable best-available final with no remaining recovery source.
    eligible_session_sql = f"""
        (
            LOCATE('{PARTIAL_FINAL_ACCEPTED}', LOWER(COALESCE(youtube_error, ''))) > 0
            OR LOCATE('{VALIDATED_FINAL_ACCEPTED}', LOWER(COALESCE(youtube_error, ''))) > 0
            OR LOWER(COALESCE(final_path, '')) LIKE '%%split_upload%%'
            OR NOT EXISTS (
                SELECT 1
                FROM recording_sessions upload_guard_session
                WHERE upload_guard_session.session_id=recordings.session_id
                  AND LOWER(COALESCE(upload_guard_session.status, '')) IN (
                    'recording','processing','merging','no_parts',
                    'postprocess_timeout','merge_failed',
                    'merge_blocked_missing_parts','exception'
                  )
            )
        )
    """
    with conn.cursor() as cur:
        # A split child must inherit its parent's exact queue position.  When
        # the oldest logical item is a split group, finish that group in part
        # order before applying the normal cross-streamer round-robin rule.
        cur.execute(
            f"""
            SELECT *
            FROM recordings
            WHERE status='done'
              AND final_path IS NOT NULL
              AND youtube_status='pending'
              AND {eligible_session_sql}
            ORDER BY {queue_priority} ASC, id ASC
            LIMIT 1
            """
        )
        oldest_row = cur.fetchone()
        if oldest_row and _is_split_child(oldest_row):
            return oldest_row, "분할 원본 순서 유지"

        if last_streamer:
            cur.execute(
                f"""
                SELECT *
                FROM recordings
                WHERE status='done'
                  AND final_path IS NOT NULL
                  AND youtube_status='pending'
                  AND {eligible_session_sql}
                  AND streamer_name <> %s
                ORDER BY {queue_priority} ASC, id ASC
                LIMIT 1
                """,
                (last_streamer,),
            )
            row = cur.fetchone()
            if row:
                return row, "직전 스트리머 제외"
        return oldest_row, "오래된 순서"


def claim_next_upload(config, worker_id):
    """Atomically reserve one pending row for one uploader worker."""
    with _QUEUE_CLAIM_LOCK:
        for _attempt in range(5):
            with db_connect(config) as conn:
                row, reason = select_next_upload(conn, config)
                if not row:
                    return None, None
                rec_id = row.get("id")
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE recordings
                        SET youtube_status='uploading',
                            youtube_started_at=NOW(),
                            youtube_error=LEFT(CONCAT(
                                %s,
                                CASE
                                    WHEN youtube_error IS NULL OR youtube_error='' THEN ''
                                    ELSE CONCAT(' | ', youtube_error)
                                END
                            ), 1000)
                        WHERE id=%s
                          AND youtube_status='pending'
                          AND status='done'
                          AND final_path IS NOT NULL
                        """,
                        (f"Claimed by uploader worker {worker_id}", rec_id),
                    )
                    if cur.rowcount == 1:
                        return get_row_by_id(conn, rec_id) or row, reason
        return None, None


def sanitize_youtube_title(value, fallback="CHZZK Recording"):
    """Return a YouTube-safe, non-empty title.

    YouTube rejects angle brackets in video titles with ``invalidTitle``.
    Broadcast titles come from upstream services, so enforce this at the API
    boundary as well as in ``make_title``.
    """
    title = str(value or "")
    try:
        title = fix_mojibake_text(title)
    except Exception:
        pass
    title = title.replace("<", "[").replace(">", "]")
    title = " ".join(title.replace("\r", " ").replace("\n", " ").split())
    title = "".join(ch for ch in title if ord(ch) >= 32 and ord(ch) != 127).strip()
    return (title or fallback)[:100]


def make_title(config, row):
    broadcast_title = str(row.get("broadcast_title") or "").strip()
    streamer = str(row.get("streamer_name") or "").strip()
    started = str(row.get("started_at") or "").strip()
    date_match = re.search(r"(20\d{2}-\d{2}-\d{2})", started)
    date_text = date_match.group(1) if date_match else ""
    bad_titles = {"stitched", "stitched_gdrive", "fixed", "merged", "gdrive", "drive_merged"}
    if broadcast_title and broadcast_title.lower() not in bad_titles:
        pieces = [broadcast_title]
        if streamer and streamer not in broadcast_title:
            pieces.append(streamer)
        if date_text:
            pieces.append(date_text)
        return sanitize_youtube_title(" | ".join(pieces))
    try:
        final_path = row.get("final_path")
        if final_path:
            stem = Path(final_path).stem
            if stem:
                return sanitize_youtube_title(stem)
    except Exception:
        pass
    template = config.get("youtube", {}).get("title_template", "{streamer_name}_{started_at}")
    return sanitize_youtube_title(template.format(
        streamer_name=row.get("streamer_name") or "unknown",
        started_at=str(row.get("started_at") or "").replace(":", "-"),
        id=row.get("id"),
    ))


def make_description(config, row):
    upload_title = make_title(config, row)

    streamer = str(row.get("streamer_name") or "").strip()
    broadcast_title = str(row.get("broadcast_title") or "").strip()
    final_path = str(row.get("final_path") or "").strip()

    bad_titles = {
        "stitched", "stitched_gdrive", "fixed", "merged",
        "gdrive", "gdrive1", "drive_merged", "gdrive_recovered"
    }

    if (
        broadcast_title in bad_titles
        or re.fullmatch(r"part\d+", broadcast_title or "", re.I)
        or re.fullmatch(r"fixed_part\d+", broadcast_title or "", re.I)
        or re.search(r"_split_\d+$", broadcast_title or "", re.I)
        or re.search(r"_recovered$", broadcast_title or "", re.I)
        or re.search(r"_mp4_parts_manual$", broadcast_title or "", re.I)
    ):
        broadcast_title = ""

    rec_date = ""
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", upload_title or final_path)
    if m:
        rec_date = m.group(1)

    label_broadcast = "\ubc29\uc1a1 \uc81c\ubaa9"
    label_streamer = "\uc2a4\ud2b8\ub9ac\uba38"
    label_date = "\ub179\ud654\uc77c"

    backup_line = "\u0043\u0048\u005a\u005a\u004b \uc790\ub3d9 \ub179\ud654 \ubc31\uc5c5 \uc601\uc0c1\uc785\ub2c8\ub2e4."
    private_line = "\uac1c\uc778 \ubcf4\uad00\uc6a9\uc73c\ub85c \uc5c5\ub85c\ub4dc\ub41c \ube44\uacf5\uac1c \uc601\uc0c1\uc785\ub2c8\ub2e4."

    lines = [
        upload_title,
        "",
    ]

    # streamer_??, [????], part/gdrive ?? ?? ??? ???? ??
    fake_broadcast_title = False
    try:
        if broadcast_title == streamer:
            fake_broadcast_title = True
        if re.fullmatch(rf"{re.escape(streamer)}_\d{{2}}-\d{{2}}-\d{{2}}", broadcast_title or ""):
            fake_broadcast_title = True
        if re.fullmatch(r"\[[^\]]+\]", broadcast_title or ""):
            fake_broadcast_title = True
    except Exception:
        pass

    if broadcast_title and not fake_broadcast_title:
        lines.append(f"{label_broadcast}: {broadcast_title}")
    if streamer:
        lines.append(f"{label_streamer}: {streamer}")
    if rec_date:
        lines.append(f"{label_date}: {rec_date}")

    lines += [
        "",
        backup_line,
        private_line,
    ]

    return "\n".join(lines)

def progress_bar(percent, width=24):
    percent = max(0, min(100, int(percent)))
    filled = int(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def sleep_retry(base_delay, attempt, max_delay=60):
    delay = min(base_delay * (2 ** max(0, attempt - 1)), max(5, int(max_delay)))
    delay += random.uniform(0, min(3, delay * 0.1))
    log(f"재시도 전 대기: {int(delay)}초", "⏳")
    time.sleep(delay)


def is_retryable_http_error(err):
    try:
        return int(err.resp.status) in RETRYABLE_HTTP_STATUS
    except Exception:
        return False


def is_video_upload_daily_quota_error(err):
    """Return True only for the separate videos.insert daily request bucket."""
    try:
        status = int(err.resp.status)
    except Exception:
        status = 0
    message = str(err or "").lower()
    return status in (400, 403, 429) and (
        "uploadlimitexceeded" in message
        or "exceeded the number of videos" in message
        or "daily upload limit" in message
        or "video uploads per day" in message
        or "quota metric 'video uploads'" in message
        or ('"video uploads"' in message and "quota" in message)
    )


def is_video_too_long_request_error(err):
    """Recognize only explicit duration failures, never generic upload limits."""
    message = str(err or "").lower()
    markers = (
        "video is too long",
        "video too long",
        "videotoolong",
        "duration is too long",
        "exceeds the maximum allowed duration",
        "exceeds maximum allowed duration",
        "invalid video length",
    )
    return any(marker in message for marker in markers)


def seconds_until_youtube_upload_quota_reset(config=None):
    """Estimate the next rolling-24-hour upload slot plus a small grace."""
    if config:
        try:
            with db_connect(config) as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT youtube_done_at
                        FROM recordings
                        WHERE youtube_video_id IS NOT NULL
                          AND youtube_video_id <> ''
                          AND youtube_done_at IS NOT NULL
                          AND youtube_done_at > NOW() - INTERVAL 24 HOUR
                          AND youtube_status IN (
                                'uploaded_pending_youtube_processing',
                                'processing', 'done'
                          )
                        ORDER BY youtube_done_at ASC
                        LIMIT 1
                    """)
                    row = cur.fetchone()
            oldest = (row or {}).get("youtube_done_at")
            if oldest:
                retry_at = oldest + timedelta(hours=24, minutes=5)
                return max(300, int((retry_at - datetime.now()).total_seconds()))
        except Exception as exc:
            log(f"YouTube 24시간 업로드 슬롯 계산 실패: {exc}", "⚠️")

    # YouTube documents this as a rolling 24-hour channel limit, not a fixed
    # Pacific-midnight API quota. With no DB history, wait a full safe window.
    return 24 * 3600 + 300


def _legacy_seconds_until_pacific_midnight():
    """Retained only as a reference for unrelated API quota reset behavior."""
    try:
        pacific = ZoneInfo("America/Los_Angeles")
        now = datetime.now(pacific)
        reset_at = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return max(60, int((reset_at - now).total_seconds()) + 300)
    except Exception:
        # Some standalone Windows Python installations do not ship the IANA
        # timezone database. Calculate the US Pacific DST offset directly so
        # the fallback still targets the real quota reset instead of sleeping
        # an arbitrary hour.
        now_utc = datetime.now(timezone.utc)
        year = now_utc.year

        def nth_sunday(month, occurrence):
            first = datetime(year, month, 1, tzinfo=timezone.utc)
            first_sunday = 1 + ((6 - first.weekday()) % 7)
            return first_sunday + (occurrence - 1) * 7

        # US Pacific: DST begins 02:00 PST (10:00 UTC) on March's second
        # Sunday and ends 02:00 PDT (09:00 UTC) on November's first Sunday.
        dst_start = datetime(year, 3, nth_sunday(3, 2), 10, tzinfo=timezone.utc)
        dst_end = datetime(year, 11, nth_sunday(11, 1), 9, tzinfo=timezone.utc)
        offset_hours = -7 if dst_start <= now_utc < dst_end else -8
        pacific_now = now_utc + timedelta(hours=offset_hours)
        reset_pacific = (pacific_now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        reset_utc = reset_pacific - timedelta(hours=offset_hours)
        return max(60, int((reset_utc - now_utc).total_seconds()) + 300)


def block_youtube_uploads_until_daily_reset(config=None):
    global _UPLOAD_QUOTA_BLOCKED_UNTIL
    wait_seconds = seconds_until_youtube_upload_quota_reset(config)
    with _UPLOAD_QUOTA_LOCK:
        _UPLOAD_QUOTA_BLOCKED_UNTIL = max(
            _UPLOAD_QUOTA_BLOCKED_UNTIL,
            time.time() + wait_seconds,
        )
    return wait_seconds


def youtube_upload_quota_wait_seconds():
    with _UPLOAD_QUOTA_LOCK:
        return max(0, int(_UPLOAD_QUOTA_BLOCKED_UNTIL - time.time()))


def sanitize_youtube_description(value):
    """Return a YouTube-safe description within the API byte limit.

    YouTube rejects descriptions containing literal angle brackets with
    ``invalidDescription``.  Broadcast titles are supplied by upstream
    platforms and are repeated in the description, so normalize them at the
    final API boundary just like video titles.
    """
    text = str(value or "")
    try:
        text = fix_mojibake_text(text)
    except Exception:
        pass
    text = text.replace("<", "[").replace(">", "]")
    text = "".join(
        ch for ch in text
        if ch in "\n\r\t" or (ord(ch) >= 0x20 and not 0xD800 <= ord(ch) <= 0xDFFF)
    )
    raw = text.encode("utf-8", errors="ignore")
    if len(raw) > 4900:
        raw = raw[:4900]
        while raw:
            try:
                text = raw.decode("utf-8")
                break
            except UnicodeDecodeError:
                raw = raw[:-1]
    return text.strip()


def parse_range_end(range_header):
    if not range_header:
        return 0
    m = re.search(r"bytes=0-(\d+)", str(range_header))
    if not m:
        return 0
    return int(m.group(1)) + 1


def query_resumable_progress(request, uri, total_size):
    headers = {"Content-Length": "0", "Content-Range": f"bytes */{total_size}"}
    resp, content = request.http.request(uri, method="PUT", body=b"", headers=headers)
    status = int(getattr(resp, "status", 0))
    if status in (200, 201):
        try:
            data = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        except Exception:
            data = {}
        return "complete", data
    if status == 308:
        return "progress", parse_range_end(resp.get("range") or resp.get("Range"))
    if status in (404, 410):
        return "expired", 0
    return "unknown", 0


def sanitize_youtube_body_v2(body):
    try:
        sn = body.get("snippet") or {}

        title = str(sn.get("title") or "").strip()
        try:
            title = fix_mojibake_text(title)
        except Exception:
            pass

        if not title:
            title = "CHZZK Recording"

        sn["title"] = title

        # description? make_description?? ?? ???? ?? ?? ????.
        desc = str(sn.get("description") or "").strip()
        if not desc:
            sn["description"] = (
                f"{title}\n\n"
                "CHZZK ?? ?? ?? ?????.\n"
                "?? ????? ???? ??? ?????.\n"
            )

        body["snippet"] = sn
    except Exception:
        pass

    return body

def create_upload_request(service, file_path, title, description, privacy_status, category_id, config):
    yt = config.get("youtube", {})
    chunk_mb = int(yt.get("upload_chunk_size_mb", 8))
    title = sanitize_youtube_title(title)
    body = {
        "snippet": {
            "title": title,
            "description": sanitize_youtube_description(description),
            "categoryId": str(category_id),
        },
        "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(str(file_path), chunksize=chunk_mb * 1024 * 1024, resumable=True)
    return service.videos().insert(part="snippet,status", body=body, media_body=media)


def update_uploaded_video_metadata(service, video_id, title, description, category_id):
    """Apply a queue title edited while the resumable upload was running."""
    body = {
        "id": video_id,
        "snippet": {
            "title": sanitize_youtube_title(title),
            "description": sanitize_youtube_description(description),
            "categoryId": str(category_id),
        },
    }
    return service.videos().update(part="snippet", body=body).execute()


def upload_video(service, file_path, title, description, privacy_status, category_id, config, row):
    yt = config.get("youtube", {})
    max_retries = int(yt.get("upload_max_retries", 20))
    retry_delay = int(yt.get("upload_retry_delay_sec", 10))
    retry_max_delay = int(yt.get("upload_retry_max_delay_sec", 60))
    total_size = Path(file_path).stat().st_size
    request = create_upload_request(service, file_path, title, description, privacy_status, category_id, config)

    saved = get_resumable_session(config, row)
    if saved:
        uri = saved.get("resumable_uri")
        log("저장된 업로드 세션 발견. 이어서 상태 확인 중...", "🔄")
        try:
            kind, value = query_resumable_progress(request, uri, total_size)
            if kind == "complete":
                log("이미 YouTube 쪽 업로드가 완료된 세션으로 보입니다.", "✅")
                clear_resumable_session(config, row)
                return value if isinstance(value, dict) else {}
            if kind == "progress":
                request.resumable_uri = uri
                request.resumable_progress = int(value or 0)
                pct = int((request.resumable_progress / total_size) * 100) if total_size else 0
                log(f"이어서 업로드 재개: {pct}% 지점부터 시작", "🔄")
                update_current_progress(config, row, pct)
            else:
                log("저장된 업로드 세션이 만료/무효로 보입니다. 새 업로드로 시작합니다.", "⚠️")
                clear_resumable_session(config, row)
        except Exception as e:
            log(f"업로드 세션 확인 실패. 새 업로드로 시작합니다: {e}", "⚠️")
            clear_resumable_session(config, row)

    response = None
    retry_count = 0
    last_pct = -1
    speed_started_at = time.time()
    speed_started_bytes = int(getattr(request, "resumable_progress", 0) or 0)
    while response is None:
        try:
            status, response = request.next_chunk()
            retry_count = 0
            if request.resumable_uri:
                current_bytes = int(getattr(request, "resumable_progress", 0) or 0)
                current_pct = int((current_bytes / total_size) * 100) if total_size else 0
                save_resumable_session(config, row, request.resumable_uri, current_bytes, current_pct)
            if status:
                pct = int(status.progress() * 100)
                if pct != last_pct:
                    progress_bytes = int(getattr(status, "resumable_progress", 0) or int(total_size * status.progress()))
                    save_resumable_session(config, row, request.resumable_uri, progress_bytes, pct)
                    elapsed = max(0.001, time.time() - speed_started_at)
                    transferred = max(0, progress_bytes - speed_started_bytes)
                    speed_mbps = transferred * 8 / elapsed / 1_000_000
                    remaining = max(0, total_size - progress_bytes)
                    eta_sec = int(remaining * 8 / (speed_mbps * 1_000_000)) if speed_mbps > 0 else 0
                    speed_text = f" / {speed_mbps:.1f}Mbps"
                    if eta_sec > 0:
                        speed_text += f" / 예상 {format_seconds(eta_sec)}"
                    log(f"업로드 진행률: {pct:3d}%  {progress_bar(pct)}{speed_text}", "📤")
                    update_current_progress(config, row, pct)
                    last_pct = pct
            sched = upload_schedule_config(config)
            if sched.get("enabled") and sched.get("pause_active_upload") and not is_upload_allowed_now(config):
                if request.resumable_uri:
                    current_bytes = int(getattr(request, "resumable_progress", 0) or 0)
                    current_pct = int((current_bytes / total_size) * 100) if total_size else 0
                    save_resumable_session(config, row, request.resumable_uri, current_bytes, current_pct)
                wait_sec = seconds_until_next_allowed(config)
                raise UploadPausedBySchedule(
                    f"업로드 허용 시간이 지나 일시중지합니다. 다음 허용 시간까지 약 {format_seconds(wait_sec)}"
                )
        except HttpError as e:
            if is_video_upload_daily_quota_error(e):
                if request.resumable_uri:
                    current_bytes = int(getattr(request, "resumable_progress", 0) or 0)
                    current_pct = int((current_bytes / total_size) * 100) if total_size else 0
                    save_resumable_session(
                        config, row, request.resumable_uri, current_bytes, current_pct
                    )
                wait_seconds = block_youtube_uploads_until_daily_reset(config)
                raise UploadDailyQuotaExceeded(wait_seconds, e) from e
            if is_video_too_long_request_error(e):
                raise UploadVideoTooLongError(
                    f"YouTube rejected the full-length upload as too long: {e}"
                ) from e
            try:
                http_status = int(e.resp.status)
            except Exception:
                http_status = 0
            if 400 <= http_status < 500 and http_status not in RETRYABLE_HTTP_STATUS:
                raise UploadPermanentRequestError(
                    f"YouTube 영구 요청 오류 HTTP {http_status}; 자동 재시도 격리: {e}"
                ) from e
            if is_retryable_http_error(e) and retry_count < max_retries:
                retry_count += 1
                log(f"YouTube 일시 오류, 재시도 {retry_count}/{max_retries}: {e}", "⚠️")
                sleep_retry(retry_delay, retry_count, retry_max_delay)
                continue
            raise
        except RETRYABLE_EXCEPTIONS as e:
            if retry_count < max_retries:
                retry_count += 1
                log(f"네트워크 오류, 재시도 {retry_count}/{max_retries}: {type(e).__name__}: {e}", "⚠️")
                sleep_retry(retry_delay, retry_count, retry_max_delay)
                continue
            raise
    update_current_progress(config, row, 100)
    clear_resumable_session(config, row)
    return response


def _youtube_cleanup_delete_allowed(row, processing_success_confirmed=False):
    """Allow source deletion only for persisted or freshly confirmed success."""
    status = str(row.get("youtube_status") or "")
    if status == "done":
        return True
    return bool(processing_success_confirmed) and status in {
        "uploaded_pending_youtube_processing",
        "processing",
    }


def cleanup_after_youtube_processed(conn, config, row, confirmed_success=False):
    yt_config = config.get("youtube", {})
    delete_after_success = bool(yt_config.get("delete_after_success", True))
    delete_parts_after_success = bool(yt_config.get("delete_parts_after_success", True))
    rec_id = row.get("id")
    file_path = Path(row.get("final_path") or "")
    parts_dir = Path(row.get("parts_dir")) if row.get("parts_dir") else None
    deleted = 0
    if not _youtube_cleanup_delete_allowed(row, confirmed_success):
        log(
            f"YouTube 성공 미확인 파일 삭제 거부 #{rec_id}: "
            f"status={row.get('youtube_status')}; 원본/parts 보존",
            "🛡️",
        )
        return 0
    if delete_after_success and file_path.exists():
        try:
            file_path.unlink()
            deleted = 1
            log(f"YouTube 처리 확인 후 최종본 삭제 완료: {file_path}", "🧹")
            remove_empty_june_month_dir(file_path)
        except Exception as e:
            log(f"최종본 삭제 실패 #{rec_id}: {e}", "⚠️")
    if delete_parts_after_success and parts_dir and parts_dir.exists():
        if _is_split_child(row):
            _, message = _processing_cleanup_preserve_split_siblings(parts_dir)
            log(f"분할 자식 공유 폴더 보호 #{rec_id}: {message}", "🛡️")
        else:
            try:
                shutil.rmtree(parts_dir)
                log(f"YouTube 처리 확인 후 part 폴더 삭제 완료: {parts_dir}", "🧩")
            except Exception as e:
                log(f"part 폴더 삭제 실패 #{rec_id}: {e}", "⚠️")
    return deleted


def get_youtube_video_status(service, video_id):
    res = service.videos().list(part="status,processingDetails,contentDetails", id=video_id).execute()
    items = res.get("items") or []
    if not items:
        return None
    return items[0]


def normalize_youtube_reject_status(item):
    status = item.get("status") or {}
    processing = item.get("processingDetails") or {}
    upload_status = status.get("uploadStatus")
    rejection = status.get("rejectionReason") or ""
    failure = status.get("failureReason") or processing.get("processingFailureReason") or ""
    reason = rejection or failure or upload_status or "unknown"
    lowered = str(reason).lower()
    if "length" in lowered or "too" in lowered or "duration" in lowered:
        return "youtube_rejected_too_long", reason
    return "youtube_processing_failed", reason


def _processing_cleanup_deleted_video_split_reason(config, row):
    """Choose the six-hour fallback after a confirmed deleted long upload.

    YouTube can remove a rejected upload before the next one-minute status poll.
    At that point ``videos.list`` returns nothing and the uploads playlist only
    exposes a ``Deleted video`` tombstone.  Once a full-length transfer has
    already failed this way, retrying the same multi-hour file is wasteful even
    when it is below YouTube's nominal 12-hour ceiling.  Split any preserved
    original longer than the configured fallback segment (normally six hours).
    Short or unreadable files stay failed for explicit review/requeue.
    """
    yt = config.get("youtube", {}) or {}
    if not bool(yt.get("auto_split_too_long", True)):
        return None

    file_path = Path(row.get("final_path") or "")
    if not file_path.is_file():
        return None

    duration_sec = get_video_duration_sec(config, file_path)
    if duration_sec is None:
        return None

    segment_sec = int(yt.get("split_segment_seconds", 6 * 3600))
    if duration_sec <= segment_sec:
        return None

    return (
        "YouTube uploads playlist confirmed Deleted video after transfer; "
        f"preserved local duration {format_duration(duration_sec)} exceeds "
        f"fallback segment {format_duration(segment_sec)}"
    )


def _processing_cleanup_is_historical_deleted_failure(row):
    return (
        row.get("youtube_status") == "failed"
        and DELETED_UPLOAD_ERROR_MARKER.lower()
        in str(row.get("youtube_error") or "").lower()
    )


def fallback_split_after_length_rejection(config, rec_id, reason):
    """Split one preserved original after YouTube confirms a length rejection.

    This path is deliberately separate from preflight.  It must never run for
    an ordinary transport/API failure, and it clears the rejected video ID so
    the split parent cannot be mistaken for a successfully uploaded item.
    """
    yt = config.get("youtube", {}) or {}
    if not bool(yt.get("auto_split_too_long", True)):
        log(
            f"YouTube 길이 거절 #{rec_id}; 자동 fallback 분할이 꺼져 원본 보존",
            "🛡️",
        )
        return 0

    with db_connect(config) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM recordings
                WHERE id=%s
                  AND status='done'
                  AND final_path IS NOT NULL
                LIMIT 1
                """,
                (rec_id,),
            )
            row = cur.fetchone()
        if not row:
            log(f"길이 거절 fallback 대상 DB 행 없음 #{rec_id}", "⚠️")
            return 0

        file_path = Path(row.get("final_path") or "")
        if not file_path.exists():
            update_record(
                conn,
                rec_id,
                youtube_status="file_missing",
                youtube_error=f"YouTube length rejection fallback original missing: {file_path}",
            )
            log(f"길이 거절 fallback 원본 없음 #{rec_id}: {file_path}", "❌")
            return 0

        rejected_video_id = str(row.get("youtube_video_id") or "")
        duration_sec = get_video_duration_sec(config, file_path)
        if duration_sec is None:
            duration_sec = int(yt.get("max_video_duration_sec", 12 * 3600)) + 1

        clear_resumable_session(config, row)
        update_record(
            conn,
            rec_id,
            youtube_status="split_required",
            youtube_video_id=None,
            youtube_url=None,
            youtube_done_at=None,
            deleted_after_upload=0,
            youtube_error=(
                f"YouTube rejected full-length upload as too long: {reason}; "
                f"rejected_video_id={rejected_video_id or 'unknown'}; fallback split pending"
            )[:1000],
        )
        log(
            f"YouTube 길이 거절 확인 #{rec_id}: 원본 보존 후 fallback 분할",
            "✂️",
        )
        return split_too_long_video(conn, config, row, duration_sec)


def check_youtube_processing_queue(conn, service, config):
    """Check videos whose upload transfer finished but YouTube processing is not verified yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM recordings
            WHERE youtube_status IN ('uploaded_pending_youtube_processing','processing')
              AND youtube_video_id IS NOT NULL
              AND youtube_video_id <> ''
            ORDER BY id ASC
            LIMIT 5
            """
        )
        rows = cur.fetchall() or []
    for row in rows:
        rec_id = row.get("id")
        video_id = row.get("youtube_video_id")
        try:
            item = get_youtube_video_status(service, video_id)
            if not item:
                log(f"YouTube 처리 확인 #{rec_id}: video not found yet", "⏳")
                continue
            status = item.get("status") or {}
            processing = item.get("processingDetails") or {}
            upload_status = status.get("uploadStatus")
            processing_status = processing.get("processingStatus")
            log(f"YouTube 처리 상태 #{rec_id}: upload={upload_status}, processing={processing_status}", "🔎")
            if upload_status == "processed" or processing_status == "succeeded":
                deleted = cleanup_after_youtube_processed(
                    conn,
                    config,
                    row,
                    confirmed_success=True,
                )
                update_record(conn, rec_id, youtube_status="done", youtube_done_at="NOW()", deleted_after_upload=deleted, youtube_error=None)
                remember_uploaded(config, row)
                log(f"YouTube 처리 완료 확인 #{rec_id} → done", "✅")
            elif upload_status in ("rejected", "failed") or processing_status in ("failed", "terminated"):
                next_status, reason = normalize_youtube_reject_status(item)
                update_record(conn, rec_id, youtube_status=next_status, youtube_error=f"YouTube processing rejected/failed: {reason}")
                log(f"YouTube 처리 실패 #{rec_id} → {next_status}: {reason}. 원본/parts는 삭제하지 않습니다.", "❌")
                if next_status == "youtube_rejected_too_long":
                    fallback_split_after_length_rejection(config, rec_id, reason)
            else:
                update_record(conn, rec_id, youtube_status="uploaded_pending_youtube_processing", youtube_error="Waiting for YouTube processing confirmation")
        except HttpError as e:
            log(f"YouTube 처리 확인 API 오류 #{rec_id}: {e}", "⚠️")
        except Exception as e:
            log(f"YouTube 처리 확인 오류 #{rec_id}: {e}", "⚠️")



# PROCESSING_CLEANUP_THREAD_PATCH_START
def _processing_cleanup_db_connect(config):
    import pymysql

    db = config["database"]
    return pymysql.connect(
        host=db.get("host", "127.0.0.1"),
        port=int(db.get("port", 3306)),
        user=db.get("user", "recorder"),
        password=db.get("password", ""),
        database=db.get("database", "chzzk_recorder"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _processing_cleanup_is_youtube_done(video):
    status = video.get("status", {}) or {}
    processing = video.get("processingDetails", {}) or {}

    upload_status = status.get("uploadStatus") or ""
    processing_status = processing.get("processingStatus") or ""

    if upload_status == "processed":
        return True

    if processing_status == "succeeded":
        return True

    return False


def _processing_cleanup_deleted_upload_ids(service, video_ids):
    """Return missing video IDs that YouTube's uploads playlist marks deleted.

    ``videos.list`` returns no item both while an upload is temporarily not
    visible and after YouTube has removed it.  The channel uploads playlist
    keeps a tombstone named ``Deleted video`` for the latter case, which lets
    us stop polling forever without risking deletion of the local original.
    """
    remaining = {str(video_id) for video_id in (video_ids or []) if video_id}
    if not remaining:
        return set()

    channels = service.channels().list(part="contentDetails", mine=True).execute()
    channel_items = channels.get("items") or []
    if not channel_items:
        return set()

    uploads_id = (
        channel_items[0]
        .get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )
    if not uploads_id:
        return set()

    deleted = set()
    page_token = None
    for _ in range(20):
        response = service.playlistItems().list(
            part="contentDetails,snippet,status",
            playlistId=uploads_id,
            maxResults=50,
            pageToken=page_token,
        ).execute()
        for item in response.get("items") or []:
            video_id = str((item.get("contentDetails") or {}).get("videoId") or "")
            if video_id not in remaining:
                continue
            title = str((item.get("snippet") or {}).get("title") or "").strip().lower()
            if title == "deleted video":
                deleted.add(video_id)
            remaining.discard(video_id)
        if not remaining:
            break
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return deleted


def _processing_cleanup_should_log_missing(video_id, interval_seconds=1800):
    """Rate-limit temporary missing-video messages to avoid console spam."""
    now = time.monotonic()
    key = str(video_id or "")
    previous = float(_PROCESSING_MISSING_LOG_AT.get(key) or 0.0)
    if now - previous < interval_seconds:
        return False
    _PROCESSING_MISSING_LOG_AT[key] = now
    return True


def _processing_cleanup_safe_delete(path, config):
    p = Path(path or "")

    if not p.exists():
        return True, "already_missing"

    try:
        resolved = p.resolve(strict=True)
        drive_root = Path(config["paths"]["drive_dir"]).resolve(strict=True)
        split_root = Path(config.get("youtube", {}).get("split_output_dir") or "split_upload").resolve()
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            return False, "reparse_point"
        if not any(resolved != root and resolved.is_relative_to(root) for root in (drive_root, split_root)):
            return False, "not_allowed_path"
    except (OSError, KeyError, ValueError):
        return False, "not_allowed_path"

    if p.suffix.lower() not in [".mp4", ".ts", ".mkv", ".mov"]:
        return False, "not_video"

    try:
        p.unlink()
        remove_empty_june_month_dir(p)
        return True, "deleted"
    except Exception as e:
        return False, f"delete_failed:{e}"


def _processing_cleanup_safe_delete_parts(parts_dir, config):
    """Delete an uploaded recording's parts folder inside the configured drive root."""
    p = Path(parts_dir or "")
    if not parts_dir:
        return True, "no_parts_dir"
    if not p.exists():
        return True, "parts_already_missing"
    if not p.is_dir() or not p.name.lower().endswith("_parts"):
        return False, "not_parts_dir"

    try:
        root = Path(config["paths"]["drive_dir"]).resolve()
        p.resolve().relative_to(root)
    except Exception:
        return False, "parts_outside_drive_dir"

    try:
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            return False, "parts_reparse_point"
    except OSError:
        return False, "parts_stat_failed"

    try:
        shutil.rmtree(p)
        return True, "parts_deleted"
    except Exception as e:
        return False, f"parts_delete_failed:{e}"


def _processing_cleanup_preserve_split_siblings(parts_dir):
    """Never recursively delete a split directory from one child's cleanup."""
    p = Path(parts_dir or "")
    if not parts_dir or not p.exists():
        return True, "split_dir_already_missing"
    if not p.is_dir():
        return False, "split_parts_not_dir"
    try:
        if any(p.iterdir()):
            return True, "split_sibling_files_preserved"
        p.rmdir()
        return True, "empty_split_dir_removed"
    except OSError as exc:
        return False, f"split_dir_check_failed:{exc}"


def _split_parent_children_complete(parent, children):
    """Require every expected split child to be fully processed and locally cleaned."""
    error_text = str(parent.get("youtube_error") or "")
    expected = None
    match = re.search(r"verified\s+(\d+)\s+split files", error_text, re.I)
    if match:
        expected = int(match.group(1))
    else:
        match = re.search(r"children=\[([^\]]+)\]", error_text, re.I)
        if match:
            expected = len([item for item in match.group(1).split(",") if item.strip()])
    if expected is None or expected < 1 or len(children) != expected:
        return False
    return all(
        str(child.get("youtube_status") or "").lower() == "done"
        and bool(child.get("youtube_video_id"))
        and int(child.get("deleted_after_upload") or 0) == 1
        for child in children
    )


def _split_parent_children(parent, possible_children):
    parent_id = int(parent["id"])
    error_text = str(parent.get("youtube_error") or "")
    explicit_ids = set()
    explicit = re.search(r"children=\[([^\]]+)\]", error_text, re.I)
    if explicit:
        for value in explicit.group(1).split(","):
            try:
                explicit_ids.add(int(value.strip()))
            except ValueError:
                pass
    path_marker = f"_split_{parent_id}\\".casefold()
    return [
        child for child in possible_children
        if int(child.get("id") or 0) in explicit_ids
        or path_marker in str(child.get("final_path") or "").replace("/", "\\").casefold()
    ]


def reconcile_completed_split_parents_once(config, parent_ids=None):
    """Retire a long original only after all verified YouTube split children finish."""
    completed = 0
    if not bool(config.get("youtube", {}).get("delete_split_parent_after_children_success", False)):
        return completed
    wanted_parent_ids = {int(value) for value in parent_ids} if parent_ids is not None else None
    conn = _processing_cleanup_db_connect(config)
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, final_path, parts_dir, youtube_error
                FROM recordings
                WHERE youtube_status='split_parent_ready'
                ORDER BY id ASC
            """)
            parents = list(cur.fetchall() or ())
            cur.execute("""
                SELECT id, final_path, youtube_status, youtube_video_id,
                       deleted_after_upload, youtube_done_at
                FROM recordings
                WHERE LOWER(COALESCE(final_path, '')) LIKE '%%split_upload%%'
                   OR session_id LIKE '%%_yt_split_%%'
            """)
            possible_children = list(cur.fetchall() or ())
            service = None

            for parent in parents:
                parent_id = int(parent["id"])
                if wanted_parent_ids is not None and parent_id not in wanted_parent_ids:
                    continue
                children = _split_parent_children(parent, possible_children)
                if not _split_parent_children_complete(parent, children):
                    continue

                child_videos = [str(child["youtube_video_id"]) for child in children]
                try:
                    if service is None:
                        service = youtube_service(config)
                    response = service.videos().list(
                        part="status,processingDetails",
                        id=",".join(child_videos),
                    ).execute()
                    current_videos = {
                        str(item.get("id")): item for item in response.get("items", [])
                    }
                    if any(
                        video_id not in current_videos
                        or not _processing_cleanup_is_youtube_done(current_videos[video_id])
                        for video_id in child_videos
                    ):
                        log(
                            f"[SPLIT_PARENT_CLEANUP] #{parent_id} YouTube 현재 상태 미완료/누락; "
                            "부모 원본 보존",
                            "CLEANUP",
                        )
                        continue
                except Exception as exc:
                    log(
                        f"[SPLIT_PARENT_CLEANUP] #{parent_id} YouTube 재검증 실패; "
                        f"부모 원본 보존: {exc}",
                        "CLEANUP",
                    )
                    continue

                ok, delete_message = _processing_cleanup_safe_delete(parent.get("final_path"), config)
                if not ok:
                    log(
                        f"[SPLIT_PARENT_CLEANUP] #{parent_id} 원본 삭제 재시도 대기: {delete_message}",
                        "CLEANUP",
                    )
                    continue

                child_ids = [int(child["id"]) for child in children]
                cur.execute("""
                    UPDATE recordings
                    SET youtube_status='done',
                        youtube_done_at=COALESCE(youtube_done_at, NOW()),
                        deleted_after_upload=1,
                        youtube_error=%s
                    WHERE id=%s AND youtube_status='split_parent_ready'
                """, (
                    f"split parent cleanup: children={child_ids}; videos={child_videos}; "
                    f"local_delete={delete_message}",
                    parent_id,
                ))
                completed += 1
                log(
                    f"[SPLIT_PARENT_CLEANUP] #{parent_id} 분할본 {len(children)}개 처리 완료 확인; "
                    f"원본 정리={delete_message}",
                    "CLEANUP",
                )
    return completed


def processing_cleanup_once(config):
    """
    YouTube ?? ??? ?? ??? ??? ?? ??? ?? ??? ???? ?????.
    uploaded_pending_youtube_processing ??? YouTube ?? ?? ?? ? done ??.
    done?? deleted_after_upload=0? ??? ??? ??.
    """
    from pathlib import Path

    length_rejection_fallbacks = []
    try:
        service = youtube_service(config)
    except Exception as e:
        log(f"[PROCESSING_CLEANUP] YouTube service error: {e}", "CLEANUP")
        return

    try:
        conn = _processing_cleanup_db_connect(config)

        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, streamer_name, youtube_status, youtube_video_id,
                           youtube_url, final_path, parts_dir, deleted_after_upload,
                           youtube_error
                    FROM recordings
                    WHERE final_path IS NOT NULL
                      AND COALESCE(deleted_after_upload, 0) = 0
                      AND (
                            youtube_status='uploaded_pending_youtube_processing'
                          OR youtube_status='done'
                          OR (
                                youtube_status='failed'
                            AND youtube_error LIKE %s
                          )
                      )
                    ORDER BY CASE youtube_status
                                WHEN 'uploaded_pending_youtube_processing' THEN 0
                                WHEN 'done' THEN 1
                                ELSE 2
                             END,
                             id ASC
                    LIMIT 50
                """, (f"{DELETED_UPLOAD_ERROR_MARKER}%",))
                rows = cur.fetchall()

                if not rows:
                    return

                # uploaded_pending_youtube_processing? YouTube ?? ?? ?? ?? ??
                video_ids = [
                    r["youtube_video_id"]
                    for r in rows
                    if r.get("youtube_status") == "uploaded_pending_youtube_processing"
                    and r.get("youtube_video_id")
                ]

                video_map = {}

                for i in range(0, len(video_ids), 50):
                    batch = video_ids[i:i+50]
                    if not batch:
                        continue

                    res = service.videos().list(
                        part="status,processingDetails,contentDetails",
                        id=",".join(batch)
                    ).execute()

                    for item in res.get("items", []):
                        video_map[item["id"]] = item

                missing_video_ids = [video_id for video_id in video_ids if video_id not in video_map]
                deleted_video_ids = set()
                if missing_video_ids:
                    try:
                        deleted_video_ids = _processing_cleanup_deleted_upload_ids(
                            service,
                            missing_video_ids,
                        )
                    except Exception as exc:
                        if _processing_cleanup_should_log_missing("deleted-check-error"):
                            log(
                                f"[PROCESSING_CLEANUP] deleted-video check failed: {exc}",
                                "CLEANUP",
                            )

                for r in rows:
                    rid = r["id"]
                    status = r.get("youtube_status")
                    vid = r.get("youtube_video_id")
                    path = r.get("final_path")
                    parts_dir = r.get("parts_dir")

                    should_delete = False
                    processing_success_confirmed = False
                    done_reason = ""

                    if status == "done":
                        should_delete = True
                        done_reason = "already_done_retry_delete"

                    elif _processing_cleanup_is_historical_deleted_failure(r):
                        fallback_reason = _processing_cleanup_deleted_video_split_reason(
                            config,
                            r,
                        )
                        if fallback_reason:
                            cur.execute("""
                                UPDATE recordings
                                SET youtube_status='split_required',
                                    youtube_error=%s
                                WHERE id=%s
                                  AND youtube_status='failed'
                            """, (
                                f"Historical deleted upload recovered as length rejection: "
                                f"{fallback_reason}",
                                rid,
                            ))
                            length_rejection_fallbacks.append((rid, fallback_reason))
                            log(
                                f"[PROCESSING_CLEANUP] #{rid} 과거 Deleted video를 "
                                "장시간 거절로 복구하여 6시간 분할 예약",
                                "CLEANUP",
                            )
                        continue

                    elif status == "uploaded_pending_youtube_processing":
                        video = video_map.get(vid)

                        if not video:
                            if vid in deleted_video_ids:
                                fallback_reason = _processing_cleanup_deleted_video_split_reason(
                                    config,
                                    r,
                                )
                                if fallback_reason:
                                    cur.execute("""
                                        UPDATE recordings
                                        SET youtube_status='split_required',
                                            youtube_error=%s
                                        WHERE id=%s
                                    """, (
                                        f"YouTube length rejection inferred from confirmed "
                                        f"Deleted video: {fallback_reason}",
                                        rid,
                                    ))
                                    length_rejection_fallbacks.append((rid, fallback_reason))
                                    log(
                                        f"[PROCESSING_CLEANUP] #{rid} Deleted video + "
                                        "12시간 초과 확인; 6시간 fallback 분할 예약",
                                        "CLEANUP",
                                    )
                                else:
                                    cur.execute("""
                                        UPDATE recordings
                                        SET youtube_status='failed',
                                            youtube_error=%s
                                        WHERE id=%s
                                    """, (
                                        f"{DELETED_UPLOAD_ERROR_MARKER}; local original "
                                        "preserved; manual requeue required",
                                        rid,
                                    ))
                                    log(
                                        f"[PROCESSING_CLEANUP] #{rid} YouTube video "
                                        "confirmed deleted; local original preserved",
                                        "CLEANUP",
                                    )
                                _PROCESSING_MISSING_LOG_AT.pop(str(vid or ""), None)
                            elif _processing_cleanup_should_log_missing(vid):
                                log(
                                    f"[PROCESSING_CLEANUP] #{rid} YouTube video not found yet: {vid}",
                                    "CLEANUP",
                                )
                            continue

                        yt_status = video.get("status", {}) or {}
                        upload_status = yt_status.get("uploadStatus") or ""

                        if _processing_cleanup_is_youtube_done(video):
                            should_delete = True
                            processing_success_confirmed = True
                            done_reason = "youtube_processing_done"

                        elif upload_status in ("failed", "rejected", "deleted"):
                            normalized_status, failure_reason = normalize_youtube_reject_status(video)
                            db_status = (
                                "split_required"
                                if normalized_status == "youtube_rejected_too_long"
                                else "failed"
                            )
                            cur.execute("""
                                UPDATE recordings
                                SET youtube_status=%s,
                                    youtube_error=%s
                                WHERE id=%s
                            """, (
                                db_status,
                                f"YouTube processing/upload failed: {failure_reason}",
                                rid,
                            ))
                            if normalized_status == "youtube_rejected_too_long":
                                length_rejection_fallbacks.append((rid, failure_reason))
                            log(
                                f"[PROCESSING_CLEANUP] #{rid} YouTube failed: "
                                f"{upload_status} / {failure_reason}",
                                "CLEANUP",
                            )
                            continue

                    if not should_delete:
                        continue

                    if not _youtube_cleanup_delete_allowed(
                        r,
                        processing_success_confirmed,
                    ):
                        log(
                            f"[PROCESSING_CLEANUP] #{rid} 성공 미확인 삭제 차단: "
                            f"status={status}; 원본/parts 보존",
                            "CLEANUP",
                        )
                        continue

                    ok, msg = _processing_cleanup_safe_delete(path, config)
                    if _is_split_child(r):
                        parts_ok, parts_msg = _processing_cleanup_preserve_split_siblings(
                            parts_dir,
                        )
                    else:
                        parts_ok, parts_msg = _processing_cleanup_safe_delete_parts(
                            parts_dir,
                            config,
                        )
                    deleted_flag = 1 if ok else 0

                    new_status = "done"
                    youtube_url = r.get("youtube_url") or (("https://www.youtube.com/watch?v=" + vid) if vid else None)

                    cur.execute("""
                        UPDATE recordings
                        SET youtube_status=%s,
                            youtube_url=COALESCE(youtube_url, %s),
                            youtube_done_at=COALESCE(youtube_done_at, youtube_started_at, NOW()),
                            deleted_after_upload=%s,
                            youtube_error=%s
                        WHERE id=%s
                    """, (
                        new_status,
                        youtube_url,
                        deleted_flag,
                        f"processing cleanup: {done_reason}; local_delete={msg}",
                        rid,
                    ))

                    log(
                        f"[PROCESSING_CLEANUP] #{rid} {done_reason} "
                        f"delete={msg} parts={parts_msg} path={path}",
                        "CLEANUP",
                    )

        for rejected_id, rejection_reason in length_rejection_fallbacks:
            try:
                fallback_split_after_length_rejection(
                    config,
                    rejected_id,
                    rejection_reason,
                )
            except Exception as exc:
                update_record_fresh(
                    config,
                    rejected_id,
                    youtube_status="split_required",
                    youtube_error=f"YouTube length rejection fallback split error: {exc}",
                )
                log(
                    f"[PROCESSING_CLEANUP] #{rejected_id} fallback split error: {exc}",
                    "CLEANUP",
                )

    except Exception as e:
        log(f"[PROCESSING_CLEANUP] error: {e}", "CLEANUP")


def start_processing_cleanup_thread(config):
    import threading
    import time

    if getattr(start_processing_cleanup_thread, "_started", False):
        return

    start_processing_cleanup_thread._started = True

    def loop():
        log("[PROCESSING_CLEANUP] background thread started", "CLEANUP")

        while True:
            try:
                reconcile_completed_split_parents_once(config)
                processing_cleanup_once(config)
            except BaseException as exc:
                # Cleanup is best-effort and must never silently kill its
                # daemon thread. Upload workers continue independently.
                log(
                    f"[PROCESSING_CLEANUP] loop survived {type(exc).__name__}: {exc}",
                    "CLEANUP",
                )
            time.sleep(60)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
# PROCESSING_CLEANUP_THREAD_PATCH_END


def main():
    if not acquire_uploader_mutex():
        print("[SINGLE_INSTANCE] YouTube uploader가 이미 실행 중입니다. 중복 실행을 종료합니다.", flush=True)
        return
    config = load_config()
    yt_config = config.get("youtube", {})
    if not yt_config.get("enabled", False):
        log("config.json에서 YouTube 업로드가 꺼져 있습니다.", "⛔")
        return
    poll = int(yt_config.get("poll_interval_sec", 60))
    delete_after_success = bool(yt_config.get("delete_after_success", True))
    delete_parts_after_success = bool(yt_config.get("delete_parts_after_success", True))
    privacy = yt_config.get("privacy_status", "private")
    category_id = yt_config.get("category_id", "20")
    service = youtube_service(config)
    start_processing_cleanup_thread(config)
    start_thumbnail_retry_thread(config, youtube_service, log)
    state = load_state(config)
    hr()
    log(APP_NAME, "🚀")
    sched = upload_schedule_config(config)
    if sched.get("enabled"):
        log(f"업로드 허용 시간: {sched.get('start')} ~ {sched.get('end')} / 시간 외 현재 청크 후 자동 일시중지", "⏰")
    else:
        log("업로드 시간 제한: 꺼짐", "⏰")
    log(f"공개 상태: {'비공개(private)' if privacy == 'private' else privacy}", "🔒")
    log(f"최종본 삭제: {'YouTube 처리 확인 후 삭제 ✅' if delete_after_success else '꺼짐 ❌'}", "🧹")
    log(f"part 폴더 삭제: {'YouTube 처리 확인 후 삭제 ✅' if delete_parts_after_success else '꺼짐 ❌'}", "🧩")
    log("업로드 방식: 원본 길이 그대로 우선 전송 + 실제 길이 거절 시에만 fallback 분할", "🔁")
    log(f"직전 업로드 스트리머: {state.get('last_uploaded_streamer') or '없음'}", "👤")
    hr()
    try:
        with db_connect(config) as conn:
            recovered = recover_stale_uploading(conn, config)
            if recovered:
                log(f"이전 uploading 항목 복구: {recovered}개 → pending", "🛠️")
            released = release_not_ready_without_sources(conn, config)
            if released:
                log(f"복구 원본이 없는 검증된 최종본 대기열 복귀: {released}개", "✅")
    except Exception as e:
        log(f"이전 업로드 복구 중 오류: {e}", "⚠️")
    while True:
        try:
            with db_connect(config) as conn:
                check_youtube_processing_queue(conn, service, config)
                released = release_not_ready_without_sources(conn, config)
                if released:
                    log(f"복구 원본이 없는 검증된 최종본 대기열 복귀: {released}개", "✅")
                counts = get_queue_counts(conn)
                if not is_upload_allowed_now(config):
                    wait_sec = seconds_until_next_allowed(config)
                    log(
                        f"업로드 제한 시간입니다. 대기 {counts['pending']} / 업로드중 {counts['uploading']} / 다음 허용까지 약 {format_seconds(wait_sec)}",
                        "⏸️",
                    )
                    time.sleep(min(poll, max(5, wait_sec)))
                    continue
                row, reason = select_next_upload(conn, config)
                if not row:
                    log(f"대기열 비어 있음 | 대기 {counts['pending']} / 업로드중 {counts['uploading']} / 완료 {counts['done']} / 실패 {counts['failed']} | {poll}초 후 재확인", "😴")
                    time.sleep(poll)
                    continue
                rec_id = row["id"]
                streamer = row.get("streamer_name") or "unknown"
                file_path = Path(row["final_path"])
                parts_dir = Path(row["parts_dir"]) if row.get("parts_dir") else None
                if not preflight_upload_safety(conn, config, row):
                    continue
                size_gb = file_path.stat().st_size / (1024 ** 3)
                update_record(conn, rec_id, youtube_status="uploading", youtube_started_at="NOW()", youtube_error=None)

                # Reload immediately before creating the YouTube request so a
                # dashboard title edit made while this item was pending wins.
                latest_row = get_row_by_id(conn, rec_id)
                if latest_row:
                    row = latest_row
                upload_title = make_title(config, row)
                upload_description = make_description(config, row)
                set_current_upload(config, row, progress=0)
                hr()
                log(f"업로드 시작 #{rec_id}", "🎬")
                log(f"스트리머: {streamer}", "👤")
                log(f"선택 방식: {reason}", "🔁")
                log(f"파일 크기: {size_gb:.2f} GB", "💾")
                log(f"파일 경로: {file_path}", "📁")
                if parts_dir:
                    log(f"part 폴더: {parts_dir}", "🧩")
                log(f"업로드 제목: {upload_title}", "🏷️")
                hr()
                try:
                    # Prepare several reviewable scene choices while the source
                    # is guaranteed to exist. The dashboard can switch the
                    # selected candidate even after YouTube processing cleanup.
                    prepare_thumbnail_variants(config, row, upload_title, log)
                    res = upload_video(
                        service, file_path, upload_title, upload_description,
                        privacy, category_id, config, row
                    )
                    video_id = res.get("id")
                    video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
                    if not video_id:
                        raise RuntimeError("YouTube upload response has no video id")

                    # A long upload can take hours. If the dashboard title was
                    # edited during transfer, update the newly created video to
                    # the latest DB value before handing it to processing.
                    try:
                        with db_connect(config) as fresh_conn:
                            latest_row = get_row_by_id(fresh_conn, rec_id) or row
                        latest_title = make_title(config, latest_row)
                        latest_description = make_description(config, latest_row)
                        if latest_title != upload_title or latest_description != upload_description:
                            update_uploaded_video_metadata(
                                service, video_id, latest_title, latest_description, category_id
                            )
                            row = latest_row
                            log(f"대시보드에서 수정된 최신 제목 반영: {latest_title}", "🏷️")
                    except Exception as metadata_error:
                        # The video itself is already uploaded. A metadata sync
                        # failure must never requeue and duplicate the upload.
                        log(f"최신 제목 후반영 실패(영상 업로드는 유지): {metadata_error}", "⚠️")

                    # Thumbnail work is isolated from the transfer.  A custom
                    # thumbnail API limit queues only the JPEG for a later retry.
                    prepare_and_apply_thumbnail(
                        config, service, row, video_id, make_title(config, row), log
                    )

                    # Do NOT delete files immediately after upload transfer. YouTube may reject later during processing.
                    update_record_fresh(
                        config,
                        rec_id,
                        youtube_status="uploaded_pending_youtube_processing",
                        youtube_video_id=video_id,
                        youtube_url=video_url,
                        # "Upload complete" on the dashboard means the file
                        # transfer finished.  Keep this timestamp while the
                        # YouTube processing verifier finishes asynchronously.
                        youtube_done_at="NOW()",
                        youtube_error="Upload transfer complete; waiting for YouTube processing confirmation",
                    )
                    clear_current_upload(config)
                    remember_uploaded(config, row)
                    hr()
                    log(f"업로드 전송 완료 #{rec_id} [{streamer}] → YouTube 처리 확인 대기", "✅")
                    log(f"YouTube URL: {video_url}", "🔗")
                    log("최종본/parts는 아직 삭제하지 않습니다. YouTube 처리 완료 확인 후 삭제합니다.", "🛡️")
                    hr()
                except UploadPausedBySchedule as e:
                    update_record_fresh(config, rec_id, youtube_status="pending", youtube_error=str(e))
                    clear_current_upload(config)
                    log(f"업로드 일시중지 #{rec_id} [{streamer}]: {e}", "⏸️")
                    time.sleep(min(poll, max(5, seconds_until_next_allowed(config))))
                except UploadDailyQuotaExceeded as e:
                    update_record_fresh(config, rec_id, youtube_status="pending", youtube_error=str(e))
                    clear_current_upload(config)
                    log(f"업로드 일일 한도 대기 #{rec_id} [{streamer}]: {e}", "⏸️")
                    time.sleep(min(300, max(5, e.wait_seconds)))
                except UploadVideoTooLongError as e:
                    update_record_fresh(
                        config,
                        rec_id,
                        youtube_status="split_required",
                        youtube_error=str(e),
                    )
                    clear_current_upload(config)
                    fallback_split_after_length_rejection(config, rec_id, str(e))
                except UploadPermanentRequestError as e:
                    update_record_fresh(config, rec_id, youtube_status="failed", youtube_error=str(e))
                    clear_current_upload(config)
                    log(
                        f"업로드 영구 오류 격리 #{rec_id} [{streamer}] → failed; "
                        "원본은 보존하고 다음 대기열로 진행합니다.",
                        "🛡️",
                    )
                except Exception as e:
                    retry_later = bool(yt_config.get("upload_requeue_on_failure", True))
                    next_status = "pending" if retry_later else "failed"
                    update_record_fresh(config, rec_id, youtube_status=next_status, youtube_error=str(e))
                    clear_current_upload(config)
                    log(f"업로드 실패 #{rec_id} [{streamer}] → {next_status}: {e}", "❌")
                    if retry_later:
                        log(f"나중에 다시 시도합니다. {poll}초 대기", "🔁")
                        time.sleep(poll)
        except KeyboardInterrupt:
            log("YouTube 업로더 종료 요청", "🛑")
            break
        except Exception as e:
            log(f"업로더 루프 오류: {e}", "⚠️")
            time.sleep(poll)


def upload_claimed_row_v2(config, worker_id, row, reason, service):
    yt_config = config.get("youtube", {})
    privacy = yt_config.get("privacy_status", "private")
    category_id = yt_config.get("category_id", "20")
    rec_id = row["id"]
    streamer = row.get("streamer_name") or "unknown"
    file_path = Path(row["final_path"])
    try:
        saved_resume = get_resumable_session(config, row)
        if saved_resume:
            # This exact file was already safety-checked and partly accepted by
            # YouTube.  Rechecking mutable recorder bookkeeping after a restart
            # can incorrectly quarantine it and abandon a valid resume URI.
            log(
                f"[W{worker_id}] 기존 업로드 세션 우선 재개 #{rec_id}: "
                f"{int(saved_resume.get('progress_percent') or 0)}%",
                "RESUME",
            )
        else:
            with db_connect(config) as conn:
                if not preflight_upload_safety(conn, config, row):
                    return
                row = get_row_by_id(conn, rec_id) or row
        upload_title = make_title(config, row)
        upload_description = make_description(config, row)
        set_current_upload(config, row, progress=0, worker_id=worker_id)
        log(f"[W{worker_id}] upload start #{rec_id} [{streamer}] / {reason}", "UPLOAD")
        prepare_thumbnail_variants(config, row, upload_title, log)
        result = upload_video(
            service, file_path, upload_title, upload_description,
            privacy, category_id, config, row,
        )
        video_id = result.get("id")
        if not video_id:
            raise RuntimeError("YouTube upload response has no video id")
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        try:
            with db_connect(config) as conn:
                latest = get_row_by_id(conn, rec_id) or row
            latest_title = make_title(config, latest)
            latest_description = make_description(config, latest)
            if latest_title != upload_title or latest_description != upload_description:
                update_uploaded_video_metadata(
                    service, video_id, latest_title, latest_description, category_id
                )
                row = latest
        except Exception as exc:
            log(f"[W{worker_id}] metadata sync warning #{rec_id}: {exc}", "WARN")
        prepare_and_apply_thumbnail(
            config, service, row, video_id, make_title(config, row), log
        )
        update_record_fresh(
            config, rec_id,
            youtube_status="uploaded_pending_youtube_processing",
            youtube_video_id=video_id,
            youtube_url=video_url,
            # Record transfer completion immediately.  processing_cleanup_once
            # preserves it with COALESCE when the video becomes fully processed.
            youtube_done_at="NOW()",
            youtube_error="Upload transfer complete; waiting for YouTube processing confirmation",
        )
        clear_current_upload(config, row)
        remember_uploaded(config, row)
        log(f"[W{worker_id}] transfer complete #{rec_id} [{streamer}]", "DONE")
    except UploadPausedBySchedule as exc:
        update_record_fresh(config, rec_id, youtube_status="pending", youtube_error=str(exc))
        clear_current_upload(config, row)
        log(f"[W{worker_id}] paused #{rec_id}: {exc}", "PAUSE")
    except UploadDailyQuotaExceeded as exc:
        update_record_fresh(config, rec_id, youtube_status="pending", youtube_error=str(exc))
        clear_current_upload(config, row)
        log(
            f"[W{worker_id}] YouTube 일일 업로드 한도 도달. "
            f"#{rec_id} 보존 후 약 {format_seconds(exc.wait_seconds)} 대기",
            "QUOTA",
        )
    except UploadVideoTooLongError as exc:
        update_record_fresh(
            config,
            rec_id,
            youtube_status="split_required",
            youtube_error=str(exc),
        )
        clear_current_upload(config, row)
        fallback_split_after_length_rejection(config, rec_id, str(exc))
    except UploadPermanentRequestError as exc:
        update_record_fresh(config, rec_id, youtube_status="failed", youtube_error=str(exc))
        clear_current_upload(config, row)
        log(
            f"[W{worker_id}] 영구 요청 오류 격리 #{rec_id} -> failed; "
            "원본 보존 후 다음 대기열 진행: " + str(exc),
            "GUARD",
        )
    except Exception as exc:
        next_status = "pending" if yt_config.get("upload_requeue_on_failure", True) else "failed"
        update_record_fresh(config, rec_id, youtube_status=next_status, youtube_error=str(exc))
        clear_current_upload(config, row)
        log(f"[W{worker_id}] failed #{rec_id} -> {next_status}: {exc}", "ERROR")


def uploader_worker_v2(config, worker_id, stop_event, service):
    poll = max(5, int((config.get("youtube") or {}).get("poll_interval_sec", 60)))
    log(f"uploader worker W{worker_id} started", "WORKER")
    while not stop_event.is_set():
        try:
            quota_wait = youtube_upload_quota_wait_seconds()
            if quota_wait > 0:
                stop_event.wait(min(quota_wait, 300))
                continue
            if not is_upload_allowed_now(config):
                stop_event.wait(min(poll, max(5, seconds_until_next_allowed(config))))
                continue
            if worker_id > desired_upload_worker_count(config):
                stop_event.wait(min(poll, 30))
                continue
            row, reason = claim_next_upload(config, worker_id)
            if not row:
                stop_event.wait(poll)
                continue
            upload_claimed_row_v2(config, worker_id, row, reason, service)
        except Exception as exc:
            log(f"[W{worker_id}] worker loop error: {exc}", "WARN")
            stop_event.wait(poll)


def queue_reconciliation_loop(config, stop_event):
    """Re-evaluate held finals without restarting uploads or retrying failures."""
    interval = max(60, int((config.get("youtube") or {}).get("queue_recheck_interval_sec", 300)))
    # Startup already runs one scan. Event.wait also makes shutdown immediate.
    while not stop_event.wait(interval):
        try:
            with db_connect(config) as conn:
                release_not_ready_without_sources(conn, config)
        except Exception as exc:
            log(f"대기열 재검사 실패 (다음 주기에 재시도): {exc}", "WARN")


def desired_upload_worker_count(config):
    """Reduce new parallel work while the recorder is handling many lives."""
    yt = config.get("youtube", {}) or {}
    configured = max(1, min(2, int(yt.get("concurrent_uploads", 2))))
    threshold = max(1, int(yt.get("reduce_to_one_at_active_recordings", 4)))
    try:
        status_file = Path(config["paths"].get("logs_dir", BASE_DIR / "logs")) / "status.json"
        status = json.loads(status_file.read_text(encoding="utf-8"))
        if len(status.get("active_recordings") or []) >= threshold:
            return 1
    except Exception:
        pass
    return configured


def main_v2():
    if not acquire_uploader_mutex():
        print("[SINGLE_INSTANCE] YouTube uploader is already running.", flush=True)
        return
    config = load_config()
    yt_config = config.get("youtube", {})
    if not yt_config.get("enabled", False):
        log("YouTube upload is disabled in config.json", "STOP")
        return
    worker_count = max(1, min(2, int(yt_config.get("concurrent_uploads", 2))))
    with db_connect(config) as conn:
        recovered = recover_stale_uploading(conn, config)
        upload_limit_recovered = recover_upload_limit_failures(conn)
        released = release_not_ready_without_sources(conn, config)
    quota_wait = 0
    if upload_limit_recovered:
        quota_wait = block_youtube_uploads_until_daily_reset(config)
    with _STATE_LOCK:
        state = load_state(config)
        state["current_uploads"] = {}
        state["current_upload"] = None
        save_state(config, state)
    log(
        f"{APP_NAME} / workers={worker_count} / recovered={recovered} / "
        f"upload_limit_recovered={upload_limit_recovered} / released={released}",
        "START",
    )
    if quota_wait:
        log(
            f"uploadLimitExceeded 복구 {upload_limit_recovered}건 → pending; "
            f"다음 24시간 슬롯까지 약 {format_seconds(quota_wait)} 대기",
            "QUOTA",
        )
    # Build separate API clients sequentially. googleapiclient/httplib2 clients
    # are not thread-safe, and sequential credential loading avoids two workers
    # racing while refreshing token.pickle after a reboot.
    services = [youtube_service(config) for _ in range(worker_count)]
    start_processing_cleanup_thread(config)
    start_thumbnail_retry_thread(config, youtube_service, log)
    stop_event = threading.Event()
    queue_maintenance = threading.Thread(
        target=queue_reconciliation_loop, args=(config, stop_event),
        name="youtube-queue-recheck", daemon=True,
    )
    queue_maintenance.start()
    workers = [
        threading.Thread(
            target=uploader_worker_v2,
            args=(config, worker_id, stop_event, services[worker_id - 1]),
            name=f"youtube-upload-{worker_id}",
            daemon=False,
        )
        for worker_id in range(1, worker_count + 1)
    ]
    for worker in workers:
        worker.start()
    try:
        while all(worker.is_alive() for worker in workers):
            time.sleep(2)
    except KeyboardInterrupt:
        log("YouTube uploader stop requested", "STOP")
    finally:
        stop_event.set()
        queue_maintenance.join(timeout=5)
        for worker in workers:
            worker.join(timeout=5)


if __name__ == "__main__":
    main_v2()


