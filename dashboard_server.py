import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from shared_json_io import read_json_shared


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
CONFIG_PATH = BASE_DIR / "config.json"
STREAMERS_PATH = BASE_DIR / "streamers.txt"
SUPERVISOR_STATUS_PATH = BASE_DIR / ".chzzk_supervisor_status.json"
SUPERVISOR_COMMAND_PATH = BASE_DIR / ".chzzk_supervisor_command.json"

app = FastAPI(title="CHZZK Recorder Dashboard")
app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

recorder_proc: subprocess.Popen | None = None
uploader_proc: subprocess.Popen | None = None
_process_cache = {"at": 0.0, "value": {"recorder": 0, "streamlink": 0}}
_storage_cache = {"at": 0.0, "value": None}
_storage_cache_lock = threading.Lock()
_storage_refreshing = False
_safety_cache = {"at": 0.0, "value": None}
_uploader_log_tail_started = False


def _uploader_console_snapshot():
    """Print active worker percentages when a dashboard console reconnects."""
    try:
        config = load_config()
        items = youtube_upload_progresses(config)
        if not items:
            print("[UPLOADER] 콘솔 로그 연결됨 · 진행 중인 업로드 없음", flush=True)
            return
        summary = " | ".join(
            f"W{item.get('worker_id') or '-'} #{item.get('recording_id')} "
            f"{item.get('progress_percent') if item.get('progress_percent') is not None else '-'}%"
            for item in items
        )
        print(f"[UPLOADER] 콘솔 로그 연결 복구 · {summary}", flush=True)
    except Exception:
        pass


def _tail_uploader_log_to_console():
    """Relay the persistent uploader log across dashboard process reloads."""
    try:
        config = load_config()
        log_path = Path(config.get("paths", {}).get("logs_dir") or BASE_DIR / "logs") / "youtube_uploader.log"
        position = log_path.stat().st_size if log_path.exists() else 0
        _uploader_console_snapshot()
        while True:
            time.sleep(1)
            if not log_path.exists():
                position = 0
                continue
            size = log_path.stat().st_size
            if size < position:  # log rotation/truncation
                position = 0
            if size == position:
                continue
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(position)
                for raw in handle:
                    line = raw.rstrip("\r\n")
                    if line:
                        print(f"[UPLOADER] {line}", flush=True)
                position = handle.tell()
    except Exception as exc:
        try:
            print(f"[UPLOADER] 로그 연결 오류: {type(exc).__name__}: {exc}", flush=True)
        except Exception:
            pass


@app.on_event("startup")
def start_uploader_console_log_tail():
    global _uploader_log_tail_started
    if _uploader_log_tail_started:
        return
    _uploader_log_tail_started = True
    threading.Thread(target=_tail_uploader_log_to_console, daemon=True, name="uploader-log-tail").start()


class StreamerIn(BaseModel):
    enabled: bool = True
    name: str
    url: str
    quality: str = "best"


class UploadQueueTitleIn(BaseModel):
    broadcast_title: str = ""


def normalize_broadcast_title(value: str) -> str:
    """Store a single-line title that is safe to pass to YouTube later."""
    title = str(value or "").replace("<", "[").replace(">", "]")
    title = " ".join(title.replace("\r", " ").replace("\n", " ").split())
    title = "".join(ch for ch in title if ord(ch) >= 32 and ord(ch) != 127).strip()
    return title[:100]


def upload_title_preview(row: dict) -> str:
    """Mirror youtube_uploader.make_title closely for dashboard previews."""
    broadcast_title = normalize_broadcast_title(row.get("broadcast_title") or "")
    streamer = str(row.get("streamer_name") or "").strip()
    started_at = str(row.get("started_at") or "").strip()
    date_text = started_at[:10] if len(started_at) >= 10 else ""
    bad_titles = {"stitched", "stitched_gdrive", "fixed", "merged", "gdrive", "drive_merged"}

    if broadcast_title and broadcast_title.lower() not in bad_titles:
        pieces = [broadcast_title]
        if streamer and streamer not in broadcast_title:
            pieces.append(streamer)
        if date_text:
            pieces.append(date_text)
        return normalize_broadcast_title(" | ".join(pieces))

    path = str(row.get("final_path") or row.get("parts_dir") or row.get("temp_path") or "")
    if path:
        return normalize_broadcast_title(Path(path).stem)
    return normalize_broadcast_title(f"{streamer or 'unknown'}_{started_at.replace(':', '-')}")



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



def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def db_connect():
    config = load_config()
    db = config.get("database", {})
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


def status_age_seconds(updated_at: str):
    if not updated_at:
        return None
    try:
        dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
        return int((datetime.now() - dt).total_seconds())
    except Exception:
        return None


def latest_log_line(config):
    log_path = Path(config["paths"]["logs_dir"]) / "recorder.log"
    if not log_path.exists():
        return ""
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return lines[-1] if lines else ""




def folder_size_gb(path: Path, max_depth_files: int = 12):
    """Return directory size summary. Safe for missing folders."""
    result = {
        "path": str(path),
        "exists": path.exists(),
        "size_gb": 0.0,
        "file_count": 0,
        "free_gb": None,
        "total_gb": None,
        "used_gb": None,
        "used_percent": None,
        "drive": str(path.drive or ""),
        "files": [],
    }

    try:
        if path.exists():
            try:
                usage = shutil.disk_usage(path)
                result["free_gb"] = round(usage.free / (1024 ** 3), 2)
                result["total_gb"] = round(usage.total / (1024 ** 3), 2)
                result["used_gb"] = round(usage.used / (1024 ** 3), 2)
                result["used_percent"] = round((usage.used / usage.total) * 100, 1) if usage.total else None
            except Exception:
                result["free_gb"] = None

            total = 0
            files = []
            for p in path.rglob("*"):
                if p.is_file():
                    try:
                        st = p.stat()
                    except Exception:
                        continue
                    total += st.st_size
                    result["file_count"] += 1
                    files.append((st.st_size, st.st_mtime, p))

            result["size_gb"] = round(total / (1024 ** 3), 2)
            files.sort(key=lambda x: x[1], reverse=True)
            result["files"] = [
                {
                    "name": p.name,
                    "path": str(p),
                    "size_gb": round(size / (1024 ** 3), 3),
                    "modified_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
                for size, mtime, p in files[:max_depth_files]
            ]
    except Exception as e:
        result["error"] = str(e)

    return result


def _dashboard_local_storage_uncached(config):
    paths = config.get("paths", {})
    temp_dir = Path(paths.get("temp_dir") or paths.get("temp") or BASE_DIR / "temp")
    merged_dir = Path(paths.get("merged_dir") or BASE_DIR / "merged")
    failed_dir = Path(paths.get("failed_dir") or BASE_DIR / "failed")
    drive_dir = Path(paths.get("drive_dir") or paths.get("final_save_dir") or BASE_DIR / "merged")

    data = {
        "root": str(drive_dir),
        "temp": folder_size_gb(temp_dir),
        "merged": folder_size_gb(merged_dir),
        "failed": folder_size_gb(failed_dir),
        "drive": folder_size_gb(drive_dir),
    }

    # Backward compatibility for old dashboard_extra.js
    rec_files = data["temp"].get("files", [])
    data["recording_files"] = rec_files
    data["recording_files_count"] = data["temp"].get("file_count", 0)
    return data


def _quick_storage_snapshot(config):
    paths = config.get("paths", {})
    mapped = {
        "temp": Path(paths.get("temp_dir") or BASE_DIR / "temp"),
        "merged": Path(paths.get("merged_dir") or BASE_DIR / "merged"),
        "failed": Path(paths.get("failed_dir") or BASE_DIR / "failed"),
        "drive": Path(paths.get("drive_dir") or BASE_DIR / "merged"),
    }
    data = {"root": str(mapped["drive"])}
    for name, path in mapped.items():
        item = {"path": str(path), "exists": path.exists(), "size_gb": None,
                "file_count": None, "files": [], "drive": str(path.drive or "")}
        try:
            usage = shutil.disk_usage(path)
            item.update(free_gb=round(usage.free / 1024**3, 2), total_gb=round(usage.total / 1024**3, 2),
                        used_gb=round(usage.used / 1024**3, 2),
                        used_percent=round(usage.used / usage.total * 100, 1) if usage.total else None)
        except Exception:
            pass
        data[name] = item
    data["recording_files"] = []
    data["recording_files_count"] = None
    return data


def _refresh_storage_cache(config):
    global _storage_refreshing
    try:
        value = _dashboard_local_storage_uncached(config)
        with _storage_cache_lock:
            _storage_cache.update(at=time.time(), value=value)
    finally:
        _storage_refreshing = False


def dashboard_local_storage(config, max_age_sec=300):
    """Return immediately; refresh expensive multi-terabyte scans in the background."""
    global _storage_refreshing
    now = time.time()
    with _storage_cache_lock:
        value = _storage_cache["value"]
        if value is None:
            value = _quick_storage_snapshot(config)
            _storage_cache.update(at=0.0, value=value)
        stale = now - _storage_cache["at"] >= max_age_sec
        if stale and not _storage_refreshing:
            _storage_refreshing = True
            threading.Thread(target=_refresh_storage_cache, args=(config,), daemon=True).start()
        return value


def parse_streamers():
    if not STREAMERS_PATH.exists():
        STREAMERS_PATH.write_text("", encoding="utf-8")

    items = []
    for index, line in enumerate(STREAMERS_PATH.read_text(encoding="utf-8").splitlines()):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        parts = [p.strip() for p in raw.split(",")]

        if len(parts) >= 4 and parts[0] in ["0", "1"]:
            enabled = parts[0] == "1"
            name, url, quality = parts[1], parts[2], parts[3]
        elif len(parts) >= 2:
            enabled = True
            name = parts[0]
            url = parts[1]
            quality = parts[2] if len(parts) >= 3 else "best"
        else:
            continue

        items.append({
            "index": index,
            "enabled": enabled,
            "name": name,
            "url": url,
            "quality": quality,
        })

    return items


def write_streamers(items):
    header = [
        "# 치지직 녹화할 스트리머 목록",
        "# 형식: 사용여부,이름,치지직_라이브_URL,화질",
        "# 1 = 사용, 0 = 비활성",
        "",
    ]
    lines = header + [
        f'{1 if it["enabled"] else 0},{it["name"]},{it["url"]},{it["quality"]}'
        for it in items
    ]
    STREAMERS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def youtube_summary():
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      SUM(CASE WHEN youtube_status='pending' THEN 1 ELSE 0 END) AS pending,
                      SUM(CASE WHEN youtube_status IN (
                        'pending','not_ready','failed','file_missing','split_required'
                      ) THEN 1 ELSE 0 END) AS queue_waiting,
                      SUM(CASE WHEN youtube_status='uploading' THEN 1 ELSE 0 END) AS uploading,
                      SUM(CASE WHEN youtube_status IN ('done','uploaded_pending_youtube_processing') THEN 1 ELSE 0 END) AS done,
                      SUM(CASE WHEN youtube_status='uploaded_pending_youtube_processing' THEN 1 ELSE 0 END) AS processing_pending,
                      SUM(CASE WHEN youtube_status='not_ready' THEN 1 ELSE 0 END) AS not_ready,
                      SUM(CASE WHEN youtube_status IN ('failed','file_missing') THEN 1 ELSE 0 END) AS failed
                    FROM recordings
                """)
                row = cur.fetchone() or {}
        return {k: int(row.get(k) or 0) for k in ["pending", "queue_waiting", "uploading", "done", "processing_pending", "not_ready", "failed"]}
    except Exception:
        return {"pending": 0, "queue_waiting": 0, "uploading": 0, "done": 0, "processing_pending": 0, "not_ready": 0, "failed": 0}


def youtube_daily_stats():
    """Summarize confirmed YouTube uploads by local calendar day."""
    empty = {
        "today_count": 0,
        "today_gb": 0.0,
        "yesterday_count": 0,
        "yesterday_gb": 0.0,
        "last_7d_count": 0,
        "last_7d_gb": 0.0,
        "avg_7d_count": 0.0,
        "avg_7d_gb": 0.0,
        "today_added_gb": 0.0,
        "today_deleted_gb": 0.0,
        "today_net_gb": 0.0,
        "avg_7d_added_gb": 0.0,
        "avg_7d_deleted_gb": 0.0,
    }
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND DATE(COALESCE(youtube_done_at, youtube_started_at)) = CURDATE()
                            THEN 1 ELSE 0
                          END) AS today_count,
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND DATE(COALESCE(youtube_done_at, youtube_started_at)) = CURDATE()
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS today_gb,
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND DATE(COALESCE(youtube_done_at, youtube_started_at)) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
                            THEN 1 ELSE 0
                          END) AS yesterday_count,
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND DATE(COALESCE(youtube_done_at, youtube_started_at)) = DATE_SUB(CURDATE(), INTERVAL 1 DAY)
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS yesterday_gb,
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND COALESCE(youtube_done_at, youtube_started_at) >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                             AND COALESCE(youtube_done_at, youtube_started_at) < CURDATE()
                            THEN 1 ELSE 0
                          END) AS last_7d_count,
                      SUM(CASE
                            WHEN youtube_status IN ('done','uploaded_pending_youtube_processing')
                             AND COALESCE(youtube_done_at, youtube_started_at) >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                             AND COALESCE(youtube_done_at, youtube_started_at) < CURDATE()
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS last_7d_gb,
                      SUM(CASE
                            WHEN status = 'done'
                             AND DATE(COALESCE(ended_at, created_at)) = CURDATE()
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS today_added_gb,
                      SUM(CASE
                            WHEN youtube_status = 'done'
                             AND DATE(youtube_done_at) = CURDATE()
                             AND COALESCE(deleted_after_upload, 0) = 1
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS today_deleted_gb,
                      SUM(CASE
                            WHEN status = 'done'
                             AND COALESCE(ended_at, created_at) >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                             AND COALESCE(ended_at, created_at) < CURDATE()
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS last_7d_added_gb,
                      SUM(CASE
                            WHEN youtube_status = 'done'
                             AND youtube_done_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
                             AND youtube_done_at < CURDATE()
                             AND COALESCE(deleted_after_upload, 0) = 1
                            THEN COALESCE(file_size_mb, 0) ELSE 0
                          END) / 1024 AS last_7d_deleted_gb
                    FROM recordings
                """)
                row = cur.fetchone() or {}

        last_7d_count = int(row.get("last_7d_count") or 0)
        last_7d_gb = float(row.get("last_7d_gb") or 0)
        today_added_gb = float(row.get("today_added_gb") or 0)
        today_deleted_gb = float(row.get("today_deleted_gb") or 0)
        last_7d_added_gb = float(row.get("last_7d_added_gb") or 0)
        last_7d_deleted_gb = float(row.get("last_7d_deleted_gb") or 0)
        return {
            "today_count": int(row.get("today_count") or 0),
            "today_gb": round(float(row.get("today_gb") or 0), 2),
            "yesterday_count": int(row.get("yesterday_count") or 0),
            "yesterday_gb": round(float(row.get("yesterday_gb") or 0), 2),
            "last_7d_count": last_7d_count,
            "last_7d_gb": round(last_7d_gb, 2),
            # Use the previous seven complete calendar days so the average
            # does not look artificially low early in the current day.
            "avg_7d_count": round(last_7d_count / 7, 1),
            "avg_7d_gb": round(last_7d_gb / 7, 2),
            "today_added_gb": round(today_added_gb, 2),
            "today_deleted_gb": round(today_deleted_gb, 2),
            "today_net_gb": round(today_added_gb - today_deleted_gb, 2),
            "avg_7d_added_gb": round(last_7d_added_gb / 7, 2),
            "avg_7d_deleted_gb": round(last_7d_deleted_gb / 7, 2),
        }
    except Exception:
        return empty


def youtube_upload_progresses(config=None):
    """Read every active uploader worker progress entry."""
    config = config or load_config()
    state_path = Path(config.get("paths", {}).get("logs_dir") or BASE_DIR / "logs") / "youtube_uploader_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        warn_after = max(300, int((config.get("youtube") or {}).get("upload_stall_warn_sec", 600)))
        uploads = state.get("current_uploads") or {}
        if not uploads and state.get("current_upload"):
            legacy = state.get("current_upload") or {}
            uploads = {str(legacy.get("id")): legacy}
        result = []
        for current in uploads.values():
            rec_id = current.get("id")
            resumable = (state.get("resumable_sessions") or {}).get(str(rec_id), {})
            total_bytes = int(current.get("file_size") or resumable.get("file_size") or 0)
            progress = int(current.get("progress") or resumable.get("progress_percent") or 0)
            progress = max(0, min(100, progress))
            progress_bytes = int(resumable.get("progress_bytes") or 0)
            if progress_bytes <= 0 and total_bytes > 0:
                progress_bytes = int(total_bytes * progress / 100)
            updated_at = current.get("updated_at") or resumable.get("updated_at")
            age_sec = status_age_seconds(updated_at)
            result.append({
                "recording_id": rec_id,
                "worker_id": current.get("worker_id"),
                "streamer_name": current.get("streamer_name"),
                "progress_percent": progress,
                "progress_bytes": progress_bytes,
                "total_bytes": total_bytes,
                "transferred_gb": round(progress_bytes / (1024 ** 3), 2),
                "total_gb": round(total_bytes / (1024 ** 3), 2),
                "started_at": current.get("started_at"),
                "updated_at": updated_at,
                "age_sec": age_sec,
                "stalled": age_sec is not None and age_sec >= warn_after,
            })
        return sorted(result, key=lambda item: int(item.get("worker_id") or 99))
    except Exception:
        return []


def youtube_upload_progress(config=None):
    """Backward-compatible aggregate plus the complete worker list."""
    items = youtube_upload_progresses(config)
    empty = {
        "recording_id": None, "streamer_name": None,
        "progress_percent": None, "progress_bytes": None,
        "total_bytes": None, "transferred_gb": None, "total_gb": None,
        "started_at": None, "updated_at": None, "age_sec": None,
        "stalled": False, "items": [],
    }
    if not items:
        return empty
    primary = dict(items[0])
    primary["items"] = items
    primary["stalled"] = any(bool(item.get("stalled")) for item in items)
    return primary


def youtube_uploader_log_status(config=None):
    """Return a small persistent uploader-log summary for the dashboard."""
    config = config or load_config()
    log_path = Path(config.get("paths", {}).get("logs_dir") or BASE_DIR / "logs") / "youtube_uploader.log"
    empty = {
        "path": str(log_path),
        "last_line": None,
        "last_error": None,
        "last_error_at": None,
        "last_error_age_sec": None,
    }
    try:
        if not log_path.exists():
            return empty
        with log_path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 128 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        lines = [line.strip() for line in lines if line.strip()]
        if not lines:
            return empty
        error_tokens = (
            "네트워크 오류", "YouTube 일시 오류", "업로드 실패",
            "API 오류", "HttpError", "invalid_grant", "quota",
        )
        last_error = next(
            (line for line in reversed(lines) if any(token.lower() in line.lower() for token in error_tokens)),
            None,
        )
        error_at = last_error[1:20] if last_error and last_error.startswith("[") and len(last_error) >= 20 else None
        return {
            "path": str(log_path),
            "last_line": lines[-1],
            "last_error": last_error,
            "last_error_at": error_at,
            "last_error_age_sec": status_age_seconds(error_at) if error_at else None,
        }
    except Exception:
        return empty


def process_summary(status_data):
    """Count real recorder/streamlink processes without changing process state."""
    now = time.time()
    if now - _process_cache["at"] < 15:
        return _process_cache["value"]
    value = {
        "recorder": 1 if status_data.get("engine_state") == "running" else 0,
        "streamlink": len(status_data.get("active_recordings") or []),
        "uploader": 0,
        "source": "heartbeat",
    }
    try:
        script = (
            "$p=Get-CimInstance Win32_Process; "
            "$r=@($p|?{$_.CommandLine -match 'recorder_v07\\.py'}).Count; "
            "$s=@($p|?{$_.CommandLine -match 'streamlink' -and $_.CommandLine -match ' -o ' -and $_.CommandLine -notmatch '--stream-url'}).Count; "
            "$u=@($p|?{$_.CommandLine -match 'youtube_uploader\\.py'}).Count; "
            "Write-Output \"$r,$s,$u\""
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if completed.returncode == 0:
            recorder_count, streamlink_count, uploader_count = completed.stdout.strip().split(",", 2)
            value = {"recorder": int(recorder_count), "streamlink": int(streamlink_count),
                     "uploader": int(uploader_count), "source": "windows"}
    except Exception:
        pass
    _process_cache.update(at=now, value=value)
    return value


def operations_safety_summary(config, status_data):
    """Read-only operational checks used by the dashboard safety panel."""
    now = time.time()
    if _safety_cache["value"] is not None and now - _safety_cache["at"] < 30:
        return _safety_cache["value"]
    started = time.perf_counter()
    summary = {"db_ok": False, "db_latency_ms": None, "processing": 0,
               "duplicate_final_paths": 0, "protected_split_items": 0, "stale_ts": 0}
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                summary["db_ok"] = bool(cur.fetchone())
                cur.execute("""
                    SELECT
                      SUM(CASE WHEN youtube_status IN
                        ('uploaded_pending_youtube_processing','processing','waiting_chat')
                        THEN 1 ELSE 0 END) AS processing,
                      SUM(CASE WHEN final_path LIKE %s AND youtube_status IN
                        ('pending','uploading','uploaded_pending_youtube_processing','processing','waiting_chat')
                        THEN 1 ELSE 0 END) AS protected_split_items
                    FROM recordings
                """, ("%split_upload%",))
                row = cur.fetchone() or {}
                summary["processing"] = int(row.get("processing") or 0)
                summary["protected_split_items"] = int(row.get("protected_split_items") or 0)
                cur.execute("""
                    SELECT COUNT(*) AS cnt FROM (
                      SELECT final_path FROM recordings
                      WHERE youtube_status IN
                        ('pending','uploading','uploaded_pending_youtube_processing','processing','waiting_chat','done')
                        AND final_path IS NOT NULL AND final_path <> ''
                      GROUP BY final_path HAVING COUNT(*) > 1
                    ) duplicates
                """)
                summary["duplicate_final_paths"] = int((cur.fetchone() or {}).get("cnt") or 0)
    except Exception as exc:
        summary["db_error"] = str(exc)
    finally:
        summary["db_latency_ms"] = round((time.perf_counter() - started) * 1000)
    try:
        temp_dir = Path(config["paths"]["temp_dir"])
        active_paths = {str(Path(item.get("ts_path", "")).resolve()).lower()
                        for item in status_data.get("active_recordings", []) if item.get("ts_path")}
        cutoff = time.time() - 2 * 60 * 60
        summary["stale_ts"] = sum(1 for path in temp_dir.rglob("*.ts")
                                  if path.stat().st_mtime < cutoff
                                  and str(path.resolve()).lower() not in active_paths)
    except Exception:
        pass
    _safety_cache.update(at=now, value=summary)
    return summary


def db_live_activity(limit: int = 5):
    """Small, direct DB snapshot for the dashboard's live activity strip."""
    limit = max(1, min(int(limit), 10))
    started = time.perf_counter()
    empty = {
        "ok": False,
        "latency_ms": None,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "recording": 0,
            "merging": 0,
            "pending": 0,
            "uploading": 0,
            "processing": 0,
            "done_today": 0,
        },
        "recent": [],
    }
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      (SELECT COUNT(*) FROM recording_sessions
                        WHERE status='recording' AND ended_at IS NULL
                          AND started_at >= DATE_SUB(NOW(), INTERVAL 48 HOUR)) AS recording,
                      (SELECT COUNT(*) FROM recording_sessions
                        WHERE status='merging'
                          AND COALESCE(ended_at, started_at, created_at)
                              >= DATE_SUB(NOW(), INTERVAL 48 HOUR)) AS merging,
                      (SELECT COUNT(*) FROM recordings
                        WHERE youtube_status='pending') AS pending,
                      (SELECT COUNT(*) FROM recordings
                        WHERE youtube_status='uploading') AS uploading,
                      (SELECT COUNT(*) FROM recordings
                        WHERE youtube_status IN
                          ('uploaded_pending_youtube_processing','processing','waiting_chat')) AS processing,
                      (SELECT COUNT(*) FROM recordings
                        WHERE youtube_status='done' AND DATE(youtube_done_at)=CURDATE()) AS done_today
                """)
                count_row = cur.fetchone() or {}
                cur.execute("""
                    SELECT event_at, source_kind, row_id, streamer_name, event_label, state_value
                    FROM (
                      SELECT
                        COALESCE(ended_at, started_at, created_at) AS event_at,
                        'session' AS source_kind,
                        id AS row_id,
                        streamer_name,
                        CASE
                          WHEN ended_at IS NULL AND status='recording' THEN '녹화 시작'
                          WHEN status IN ('merged','stitched','done') THEN '병합 완료'
                          WHEN status='merging' THEN '병합 시작'
                          WHEN status LIKE 'merge_failed%%' OR status='merge_blocked_missing_parts' THEN '병합 확인 필요'
                          ELSE '녹화 종료'
                        END AS event_label,
                        COALESCE(status, '-') AS state_value
                      FROM recording_sessions
                      WHERE COALESCE(ended_at, started_at, created_at) IS NOT NULL

                      UNION ALL

                      SELECT
                        created_at AS event_at,
                        'recording' AS source_kind,
                        id AS row_id,
                        streamer_name,
                        '완성본 등록' AS event_label,
                        COALESCE(youtube_status, status, '-') AS state_value
                      FROM recordings
                      WHERE created_at IS NOT NULL

                      UNION ALL

                      SELECT
                        youtube_started_at AS event_at,
                        'youtube' AS source_kind,
                        id AS row_id,
                        streamer_name,
                        '업로드 시작' AS event_label,
                        COALESCE(youtube_status, '-') AS state_value
                      FROM recordings
                      WHERE youtube_started_at IS NOT NULL

                      UNION ALL

                      SELECT
                        youtube_done_at AS event_at,
                        'youtube' AS source_kind,
                        id AS row_id,
                        streamer_name,
                        CASE
                          WHEN youtube_status='done' AND COALESCE(deleted_after_upload, 0)=1
                            THEN '처리 확인·원본 삭제'
                          WHEN youtube_status='uploaded_pending_youtube_processing'
                            THEN '전송 완료·처리 대기'
                          ELSE '업로드 상태 변경'
                        END AS event_label,
                        COALESCE(youtube_status, '-') AS state_value
                      FROM recordings
                      WHERE youtube_done_at IS NOT NULL
                    ) activity
                    ORDER BY event_at DESC, row_id DESC
                    LIMIT %s
                """, (limit,))
                recent = cur.fetchall()

        result = dict(empty)
        result["ok"] = True
        result["counts"] = {
            key: int(count_row.get(key) or 0)
            for key in empty["counts"]
        }
        result["recent"] = []
        for row in recent:
            event_at = row.get("event_at")
            result["recent"].append({
                "at": event_at.strftime("%Y-%m-%d %H:%M:%S") if event_at else None,
                "source": row.get("source_kind"),
                "id": int(row.get("row_id") or 0),
                "streamer": row.get("streamer_name") or "-",
                "event": row.get("event_label") or "상태 변경",
                "state": row.get("state_value") or "-",
            })
        result["latency_ms"] = round((time.perf_counter() - started) * 1000)
        return result
    except Exception as exc:
        empty["latency_ms"] = round((time.perf_counter() - started) * 1000)
        empty["error"] = str(exc)
        return empty


def _relay_child_output(proc: subprocess.Popen, label: str):
    """Drain a child pipe and mirror its lines to the All-in-One console safely."""
    try:
        if proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            try:
                print(f"[{label}] {line}", flush=True)
            except Exception:
                # Even if the dashboard console is replaced, keep draining the
                # pipe so the uploader cannot block or crash on console output.
                pass
    except Exception:
        pass


def start_process(script_name: str, relay_label: str | None = None):
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / script_name)],
        cwd=str(BASE_DIR),
        creationflags=creationflags,
        env=env,
        stdout=subprocess.PIPE if relay_label else subprocess.DEVNULL,
        stderr=subprocess.STDOUT if relay_label else subprocess.DEVNULL,
        text=bool(relay_label),
        encoding="utf-8" if relay_label else None,
        errors="replace" if relay_label else None,
    )
    if relay_label:
        threading.Thread(
            target=_relay_child_output,
            args=(proc, relay_label),
            daemon=True,
        ).start()
    return proc


def find_script_process_ids(script_name: str):
    """Find live Python children even after a dashboard process reload."""
    safe_name = Path(script_name).name.replace("'", "''")
    script = (
        "$self=$PID; "
        "$p=Get-CimInstance Win32_Process | "
        f"Where-Object {{$_.ProcessId -ne $self -and $_.CommandLine -match '{safe_name}'}}; "
        "$p | ForEach-Object { Write-Output $_.ProcessId }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return sorted({int(line.strip()) for line in completed.stdout.splitlines() if line.strip().isdigit()})
    except Exception:
        return []


def supervisor_status():
    try:
        data = read_json_shared(SUPERVISOR_STATUS_PATH)
        age = time.time() - float(data.get("updated_epoch") or 0)
        if age <= 12:
            data["alive"] = True
            return data
    except Exception:
        pass
    return {"alive": False, "children": {}}


def send_supervisor_command(action: str, target: str):
    status = supervisor_status()
    if not status.get("alive"):
        return False
    payload = {"action": action, "target": target, "requested_at": time.time()}
    tmp = SUPERVISOR_COMMAND_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(SUPERVISOR_COMMAND_PATH)
    return True


def stop_process(proc: subprocess.Popen | None, timeout_sec: int = 20):
    if proc is None or proc.poll() is not None:
        return True

    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)

        try:
            proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        return True

    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        return False


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


def active_capture_heartbeat_is_fresh(item, max_age_sec=90):
    """Treat a live platform capture heartbeat as stronger than a probe miss.

    SOOP's ordinary Streamlink probe can report OFF while the official-agent
    recorder is still receiving 1440p WebSocket frames.  During that window
    the dashboard used to label the whole session as finalizing even though
    the next part was actively recording.
    """
    url = str((item or {}).get("url") or "").lower()
    ts_path = str((item or {}).get("ts_path") or "").strip()
    if "sooplive.com" not in url or not ts_path:
        return False
    state_path = Path(ts_path + ".soop.json")
    try:
        if not state_path.is_file():
            return False
        if time.time() - state_path.stat().st_mtime > max(15, int(max_age_sec)):
            return False
        state = json.loads(state_path.read_text(encoding="utf-8"))
        return (
            int(state.get("video_frames") or 0) > 0
            and float(state.get("elapsed_sec") or 0) > 0
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


@app.get("/api/status")
def get_status():
    config = load_config()
    status_path = Path(config["paths"]["logs_dir"]) / "status.json"

    if status_path.exists():
        data = json.loads(status_path.read_text(encoding="utf-8"))
    else:
        data = {
            "app": "CHZZK Recorder Dashboard",
            "updated_at": "",
            "streamers_count": 0,
            "active_recordings": [],
            "last_check_results": [],
            "paths": config["paths"],
            "temp_free_gb": None,
        }

    supervisor = supervisor_status()
    supervisor_recorder_pid = (supervisor.get("children") or {}).get("RECORDER")
    running_by_process = ((recorder_proc is not None and recorder_proc.poll() is None)
                          or bool(supervisor_recorder_pid))
    proc_summary = process_summary(data)
    uploader_running = ((uploader_proc is not None and uploader_proc.poll() is None)
                        or int(proc_summary.get("uploader") or 0) > 0)
    age = status_age_seconds(data.get("updated_at", ""))
    heartbeat_alive = age is not None and age <= 120

    data["recorder_running"] = running_by_process
    data["supervisor_running"] = bool(supervisor.get("alive"))
    data["supervisor_recorder_pid"] = supervisor_recorder_pid
    data["uploader_running"] = uploader_running
    data["status_age_sec"] = age
    data["heartbeat_alive"] = heartbeat_alive
    data["engine_state"] = "running" if (running_by_process or heartbeat_alive) else "stopped"
    checks_by_name = {
        str(item.get("name") or ""): item
        for item in (data.get("last_check_results") or [])
    }
    for item in (data.get("active_recordings") or []):
        name = str(item.get("name") or "")
        capture_fresh = active_capture_heartbeat_is_fresh(item)
        check = checks_by_name.get(name)
        # A failed status probe is explicitly *not* an OFF result.  The
        # recorder keeps the active capture alive in this situation, so the
        # dashboard must not claim that finalization has started.
        check_failed = check is not None and not bool(check.get("check_ok", True))
        is_live = True if check is None else bool(check.get("is_live"))
        phase = "recording" if (is_live or check_failed or capture_fresh) else "finalizing"
        item["capture_heartbeat_fresh"] = capture_fresh
        item["status_check_failed"] = check_failed
        item["phase"] = phase
        item["phase_label"] = (
            "상태 조회 일시 실패 · 녹화 유지 중"
            if check_failed
            else "녹화 중" if phase == "recording" else "마감 처리 중"
        )
    data["latest_log_line"] = latest_log_line(config)
    data["youtube_summary"] = youtube_summary()
    data["youtube_daily_stats"] = youtube_daily_stats()
    data["upload_progress"] = youtube_upload_progress(config)
    data["uploader_log"] = youtube_uploader_log_status(config)
    data["process_summary"] = proc_summary
    data["operations_safety"] = operations_safety_summary(config, data)
    data["local_storage"] = dashboard_local_storage(config)
    data["upload_queue_preview"] = upload_queue_items(12)
    # Keep temp_free_gb useful even when recorder status.json is stale.
    try:
        drive_free = data["local_storage"]["drive"].get("free_gb")
        if drive_free is not None:
            data["temp_free_gb"] = drive_free
    except Exception:
        pass
    return data


@app.get("/api/db/activity")
def get_db_activity(limit: int = 5):
    return db_live_activity(limit)


def _thumbnail_state():
    config = load_config()
    path = Path(config.get("paths", {}).get("logs_dir") or (BASE_DIR / "logs")) / "youtube_thumbnail_state.json"
    try:
        data = read_json_shared(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {"planned": {}, "pending": {}, "completed": {}, "failed": {}}


def _thumbnail_safe_path(raw_path):
    if not raw_path:
        return None
    try:
        root = (BASE_DIR / "thumbnails").resolve()
        path = Path(str(raw_path)).resolve()
        if path == root or root not in path.parents or not path.is_file():
            return None
        return path
    except Exception:
        return None


def _thumbnail_item_path(state, recording_id=None, video_id=None):
    planned = (state.get("planned") or {}).get(str(recording_id), {}) if recording_id is not None else {}
    pending = (state.get("pending") or {}).get(str(video_id), {}) if video_id else {}
    completed = (state.get("completed") or {}).get(str(video_id), {}) if video_id else {}
    return _thumbnail_safe_path(
        pending.get("thumbnail_path")
        or planned.get("thumbnail_path")
        or completed.get("thumbnail_path")
    )


def thumbnail_dashboard_items():
    state = _thumbnail_state()
    rows = []
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, streamer_name, broadcast_title, started_at, ended_at,
                           final_path, file_size_mb, youtube_status,
                           youtube_video_id, youtube_url, youtube_error,
                           youtube_started_at, youtube_done_at
                    FROM recordings
                    WHERE youtube_status IN ('uploading','pending')
                    ORDER BY CASE WHEN youtube_status='uploading' THEN 0 ELSE 1 END, id ASC
                    LIMIT 24
                    """
                )
                active_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT id, streamer_name, broadcast_title, started_at, ended_at,
                           final_path, file_size_mb, youtube_status,
                           youtube_video_id, youtube_url, youtube_error,
                           youtube_started_at, youtube_done_at
                    FROM recordings
                    WHERE youtube_status IN ('uploaded_pending_youtube_processing','done')
                    ORDER BY COALESCE(youtube_done_at, created_at) DESC, id DESC
                    LIMIT 40
                    """
                )
                done_rows = cur.fetchall()
        seen = set()
        # PyMySQL returns tuples while some compatible cursor/test paths return
        # lists.  Normalize both result sets before combining them.
        for row in list(active_rows or ()) + list(done_rows or ()):
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            rows.append(row)
    except Exception as exc:
        return {"error": str(exc), "items": [], "summary": {}}

    planned_map = state.get("planned") or {}
    pending_map = state.get("pending") or {}
    completed_map = state.get("completed") or {}
    matched_video_ids = set()
    items = []
    for row in rows:
        for key, value in list(row.items()):
            if hasattr(value, "strftime"):
                row[key] = value.strftime("%Y-%m-%d %H:%M:%S")
        rec_id = str(row.get("id"))
        video_id = str(row.get("youtube_video_id") or "")
        if video_id:
            matched_video_ids.add(video_id)
        planned = planned_map.get(rec_id) or {}
        pending = pending_map.get(video_id) or {}
        completed = completed_map.get(video_id) or {}
        image_path = _thumbnail_item_path(state, row.get("id"), video_id)
        variants = [
            str(path) for path in (planned.get("variant_paths") or [])
            if _thumbnail_safe_path(path)
        ]
        source_exists = bool(row.get("final_path") and Path(str(row.get("final_path"))).is_file())
        if pending:
            thumb_status = "retry_pending"
        elif completed:
            thumb_status = "applied"
        elif planned and image_path:
            thumb_status = "ready"
        elif row.get("youtube_status") == "uploading":
            thumb_status = "generating"
        else:
            thumb_status = "waiting"
        image_url = None
        if image_path:
            image_url = f"/api/thumbnails/{row.get('id')}/image?v={int(image_path.stat().st_mtime)}"
        thumbnail_kind = str(planned.get("thumbnail_kind") or "")
        if not thumbnail_kind and image_path:
            thumbnail_kind = "ai" if image_path.parent.name.lower() == "ai" else "local"
        items.append({
            "recording_id": row.get("id"),
            "streamer_name": row.get("streamer_name") or "-",
            "broadcast_title": row.get("broadcast_title") or "",
            "upload_title": upload_title_preview(row),
            "started_at": row.get("started_at"),
            "youtube_status": row.get("youtube_status"),
            "youtube_video_id": video_id or None,
            "youtube_url": row.get("youtube_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else None),
            "thumbnail_status": thumb_status,
            "thumbnail_error": pending.get("last_error") or "",
            "thumbnail_kind": thumbnail_kind or "local",
            "image_url": image_url,
            "active_variant": int(planned.get("active_variant") or 0) + 1 if planned else None,
            "variant_count": len(variants),
            "source_exists": source_exists,
            "can_regenerate": bool(len(variants) > 1 or source_exists),
            "file_size_mb": row.get("file_size_mb"),
        })

    # Keep previously prepared rate-limited images visible even when their old
    # recordings fall outside the recent DB slice.
    for video_id, pending in pending_map.items():
        if video_id in matched_video_ids:
            continue
        image_path = _thumbnail_safe_path(pending.get("thumbnail_path"))
        if not image_path:
            continue
        items.append({
            "recording_id": pending.get("recording_id"),
            "streamer_name": pending.get("streamer_name") or "-",
            "broadcast_title": "",
            "upload_title": f"YouTube 영상 {video_id}",
            "started_at": None,
            "youtube_status": "uploaded",
            "youtube_video_id": video_id,
            "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_status": "retry_pending",
            "thumbnail_error": pending.get("last_error") or "",
            "thumbnail_kind": "ai" if image_path.parent.name.lower() == "ai" else "local",
            "image_url": f"/api/thumbnails/video/{video_id}/image?v={int(image_path.stat().st_mtime)}",
            "active_variant": None,
            "variant_count": 1,
            "source_exists": False,
            "can_regenerate": False,
            "file_size_mb": None,
        })

    summary = {
        "total": len(items),
        "applied": sum(1 for item in items if item["thumbnail_status"] == "applied"),
        "ready": sum(1 for item in items if item["thumbnail_status"] == "ready"),
        "retry_pending": sum(1 for item in items if item["thumbnail_status"] == "retry_pending"),
        "waiting": sum(1 for item in items if item["thumbnail_status"] in {"waiting", "generating"}),
    }
    return {"error": None, "items": items, "summary": summary, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/thumbnails")
def get_thumbnail_dashboard():
    return thumbnail_dashboard_items()


@app.get("/api/thumbnails/{recording_id}/image")
def get_thumbnail_image(recording_id: int):
    state = _thumbnail_state()
    video_id = None
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT youtube_video_id FROM recordings WHERE id=%s LIMIT 1", (recording_id,))
                row = cur.fetchone() or {}
                video_id = row.get("youtube_video_id")
    except Exception:
        pass
    path = _thumbnail_item_path(state, recording_id, video_id)
    if not path:
        raise HTTPException(status_code=404, detail="썸네일 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/thumbnails/video/{video_id}/image")
def get_thumbnail_image_by_video(video_id: str):
    path = _thumbnail_item_path(_thumbnail_state(), None, video_id)
    if not path:
        raise HTTPException(status_code=404, detail="썸네일 파일을 찾을 수 없습니다.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/thumbnails/{recording_id}/regenerate")
def regenerate_thumbnail(recording_id: int):
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, streamer_name, broadcast_title, started_at, ended_at,
                           final_path, file_size_mb, youtube_status,
                           youtube_video_id, youtube_url
                    FROM recordings WHERE id=%s LIMIT 1
                    """,
                    (recording_id,),
                )
                row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="녹화 항목을 찾을 수 없습니다.")

        from youtube_thumbnail import apply_or_queue_thumbnail, prepare_thumbnail_variants
        from youtube_uploader import make_title, youtube_service

        config = load_config()
        logger = lambda message, icon="THUMB": print(f"[DASHBOARD] [{icon}] {message}", flush=True)
        title = make_title(config, row)
        path = prepare_thumbnail_variants(config, row, title, logger, select_next=True)
        if not path:
            raise HTTPException(
                status_code=409,
                detail="새로 만들 원본이나 보관된 다른 썸네일 후보가 없습니다.",
            )
        video_id = str(row.get("youtube_video_id") or "")
        applied = None
        if video_id:
            service = youtube_service(config)
            applied = apply_or_queue_thumbnail(
                config, service, row, video_id, path, logger, force_apply=True
            )
        return {
            "ok": True,
            "recording_id": recording_id,
            "youtube_video_id": video_id or None,
            "applied": applied,
            "message": (
                "새 썸네일을 만들어 YouTube에 적용했습니다."
                if applied is True
                else "새 썸네일을 선택했습니다. 제한 해제 후 자동 적용합니다."
                if applied is False
                else "새 썸네일을 선택했습니다. 영상 업로드가 끝나면 자동 적용합니다."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"썸네일 다시 만들기 실패: {exc}") from exc


@app.get("/api/recordings")
def get_recordings(limit: int = 50):
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, streamer_name, quality, started_at, ended_at, status,
                           final_path, file_size_mb, error_message,
                           youtube_status, youtube_video_id, youtube_url, youtube_error,
                           youtube_started_at, youtube_done_at, deleted_after_upload,
                           created_at
                    FROM recordings
                    WHERE COALESCE(youtube_status, '') NOT IN ('not_requested','retired_missing')
                    ORDER BY id DESC
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()

        for row in rows:
            for k, v in list(row.items()):
                if hasattr(v, "strftime"):
                    row[k] = v.strftime("%Y-%m-%d %H:%M:%S")
        return rows

    except Exception as e:
        return {"error": str(e), "rows": []}



def table_columns(table_name: str):
    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SHOW COLUMNS FROM `{table_name}`")
                return {row["Field"] for row in cur.fetchall()}
    except Exception:
        return set()


def upload_queue_items(limit: int = 500, offset: int = 0):
    """Return upload/repair rows with a stable, gap-free display number.

    ``queue_order`` is a *display order* for every non-uploading row shown in
    the upload queue.  Diagnostic rows (for example ``not_ready``) must still
    receive a number so the UI never falls back to ``-``.  Their real database
    status is kept intact; numbering a held row does not make it uploadable.
    """
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 25
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0
    cols = table_columns("recordings")
    if not cols:
        return {"error": "recordings 테이블 컬럼을 확인할 수 없습니다.", "items": []}

    wanted = [
        "id", "streamer_name", "broadcast_title", "quality", "started_at", "ended_at", "status",
        "final_path", "parts_dir", "temp_path", "file_size_mb",
        "youtube_status", "youtube_error", "youtube_url",
        "youtube_started_at", "youtube_done_at", "created_at"
    ]
    select_cols = [c for c in wanted if c in cols]

    # Some old DBs may not have youtube_status. Fall back to empty list.
    if "youtube_status" not in cols:
        return {"error": "youtube_status 컬럼이 없습니다.", "items": []}

    order_col = "id" if "id" in cols else select_cols[0]
    if "id" in cols and "youtube_error" in cols:
        queue_priority_sql = """
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
    else:
        queue_priority_sql = f"`{order_col}`"
    query = f"""
        SELECT {", ".join("`" + c + "`" for c in select_cols)},
               {queue_priority_sql} AS `_queue_priority`
        FROM recordings
        WHERE youtube_status IN (
            'pending','uploading','not_ready','failed','file_missing','split_required'
        )
        ORDER BY
            CASE
                WHEN youtube_status='uploading' THEN 0
                WHEN youtube_status='pending' THEN 1
                ELSE 2
            END,
            `_queue_priority` ASC,
            `{order_col}` ASC
        LIMIT %s OFFSET %s
    """

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN youtube_status='uploading' THEN 1 ELSE 0 END)
                            AS uploading_total
                    FROM recordings
                    WHERE youtube_status IN (
                        'pending','uploading','not_ready','failed','file_missing','split_required'
                    )
                    """
                )
                count_row = cur.fetchone() or {}
                total = int(count_row.get("total") or 0)
                uploading_total = int(count_row.get("uploading_total") or 0)
                cur.execute(query, (limit, offset))
                rows = cur.fetchall()

        upload_progress = youtube_upload_progress()
        upload_progress_by_id = {
            str(item.get("recording_id")): item
            for item in (upload_progress.get("items") or [])
            if item.get("recording_id") is not None
        }
        items = []
        for idx, row in enumerate(rows):
            row.pop("_queue_priority", None)
            for k, v in list(row.items()):
                if hasattr(v, "strftime"):
                    row[k] = v.strftime("%Y-%m-%d %H:%M:%S")

            status = row.get("youtube_status") or "-"
            if status == "uploading":
                queue_order = 0
            else:
                global_position = offset + idx + 1
                queue_order = max(1, global_position - uploading_total)

            path = row.get("final_path") or row.get("parts_dir") or row.get("temp_path") or ""
            name = Path(path).name if path else ""
            title = name
            if title.lower().endswith((".mp4", ".ts", ".mkv")):
                title = title.rsplit(".", 1)[0]

            row["queue_order"] = queue_order
            item_progress = upload_progress_by_id.get(str(row.get("id")))
            if status == "uploading" and item_progress:
                row["upload_progress_percent"] = item_progress.get("progress_percent")
                row["upload_progress_bytes"] = item_progress.get("progress_bytes")
                row["upload_total_bytes"] = item_progress.get("total_bytes")
                row["upload_progress_updated_at"] = item_progress.get("updated_at")
            else:
                row["upload_progress_percent"] = None
            row["display_title"] = title or f'{row.get("streamer_name", "-")} #{row.get("id", "-")}'
            row["broadcast_title"] = str(row.get("broadcast_title") or "")
            row["upload_title_preview"] = upload_title_preview(row)
            row["upload_path"] = path
            row["upload_dir"] = str(Path(path).parent) if path else ""
            row["exists"] = bool(path and Path(path).exists())
            if path and Path(path).exists():
                try:
                    row["actual_size_gb"] = round(Path(path).stat().st_size / (1024 ** 3), 2)
                except Exception:
                    row["actual_size_gb"] = None
            else:
                row["actual_size_gb"] = None

            items.append(row)

        return {"error": None, "items": items, "total": total, "offset": offset, "limit": limit}

    except Exception as e:
        return {"error": str(e), "items": [], "total": 0, "offset": offset, "limit": limit}


@app.get("/api/upload_queue")
def get_upload_queue(page: int = 1, page_size: int = 25, limit: int | None = None):
    # ``limit`` keeps older dashboard clients compatible. New clients use
    # page/page_size so a large queue does not create one extremely tall page.
    if limit is not None:
        page = 1
        page_size = limit
    page = max(1, int(page or 1))
    page_size = max(10, min(int(page_size or 25), 50))
    result = upload_queue_items(page_size, (page - 1) * page_size)
    total = int(result.get("total") or 0)
    total_pages = max(1, (total + page_size - 1) // page_size)
    result.update({
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    })
    return result


@app.put("/api/upload_queue/{recording_id}/title")
def update_upload_queue_title(recording_id: int, item: UploadQueueTitleIn):
    """Update the title source used when this queue item reaches YouTube."""
    cols = table_columns("recordings")
    if "broadcast_title" not in cols:
        raise HTTPException(status_code=409, detail="recordings.broadcast_title 컬럼이 없습니다.")

    editable_statuses = {
        "pending", "uploading", "not_ready", "failed", "file_missing", "split_required"
    }
    title = normalize_broadcast_title(item.broadcast_title)

    try:
        with db_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, streamer_name, broadcast_title, started_at, final_path, "
                    "parts_dir, temp_path, youtube_status FROM recordings WHERE id=%s LIMIT 1",
                    (recording_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="대기열 항목을 찾을 수 없습니다.")

                status = str(row.get("youtube_status") or "")
                if status not in editable_statuses:
                    raise HTTPException(
                        status_code=409,
                        detail=f"현재 상태({status or '-'})에서는 대기열 제목을 수정할 수 없습니다.",
                    )

                cur.execute(
                    "UPDATE recordings SET broadcast_title=%s WHERE id=%s",
                    (title or None, recording_id),
                )
                row["broadcast_title"] = title

        return {
            "ok": True,
            "recording_id": recording_id,
            "broadcast_title": title,
            "upload_title_preview": upload_title_preview(row),
            "youtube_status": status,
            "message": (
                "방송 제목을 저장했습니다. 진행 중인 업로드는 전송 완료 직후 최신 제목으로 맞춥니다."
                if status == "uploading"
                else "방송 제목을 저장했습니다. 업로드 시작 시 이 제목을 사용합니다."
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"방송 제목 저장 실패: {exc}") from exc


@app.get("/api/streamers")
def get_streamers():
    return parse_streamers()


@app.post("/api/streamers")
def add_streamer(item: StreamerIn):
    if detect_platform_from_url(item.url) not in {"chzzk", "soop"}:
        raise HTTPException(status_code=400, detail="CHZZK 또는 SOOP 방송 주소만 등록할 수 있습니다.")
    items = parse_streamers()
    if any(x["name"] == item.name for x in items):
        raise HTTPException(status_code=400, detail="같은 이름의 스트리머가 이미 있습니다.")

    items.append(item.model_dump())
    write_streamers(items)
    return {"ok": True}


@app.put("/api/streamers/{name}/toggle")
def toggle_streamer(name: str):
    items = parse_streamers()
    for item in items:
        if item["name"] == name:
            item["enabled"] = not item["enabled"]
            write_streamers(items)
            return {
                "ok": True,
                "enabled": item["enabled"],
                "message": (
                    f"{name} 감시를 시작했습니다."
                    if item["enabled"]
                    else f"{name} 감시를 중지했습니다. 녹화 중이면 현재 part를 안전 종료합니다."
                ),
            }

    raise HTTPException(status_code=404, detail="스트리머를 찾을 수 없습니다.")


@app.delete("/api/streamers/{name}")
def delete_streamer(name: str):
    items = parse_streamers()
    new_items = [item for item in items if item["name"] != name]

    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail="스트리머를 찾을 수 없습니다.")

    write_streamers(new_items)
    return {"ok": True}


@app.get("/api/logs", response_class=PlainTextResponse)
def get_logs(lines: int = 120):
    config = load_config()
    log_path = Path(config["paths"]["logs_dir"]) / "recorder.log"

    if not log_path.exists():
        return ""

    return "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:])


@app.post("/api/recorder/start")
def start_recorder():
    global recorder_proc

    if send_supervisor_command("start", "RECORDER"):
        return {"ok": True, "message": "Supervisor에 녹화기 시작을 요청했습니다."}

    if recorder_proc is not None and recorder_proc.poll() is None:
        return {"ok": True, "message": "이미 실행 중입니다."}

    recorder_proc = start_process("recorder_v07.py")
    return {"ok": True, "message": "녹화기를 시작했습니다."}


@app.post("/api/recorder/stop")
def stop_recorder():
    global recorder_proc

    if send_supervisor_command("stop", "RECORDER"):
        return {"ok": True, "message": "Supervisor에 녹화기 종료를 요청했습니다."}

    if recorder_proc is None or recorder_proc.poll() is not None:
        recorder_proc = None
        return {"ok": True, "message": "실행 중이 아닙니다."}

    stop_process(recorder_proc, timeout_sec=20)
    recorder_proc = None
    return {"ok": True, "message": "녹화기 종료 요청을 보냈습니다."}


@app.post("/api/recorder/reload")
def reload_recorder():
    """
    대시보드에서 즉시 녹화기를 재시작하는 기능.
    config.json / streamers.txt / recorder_v07.py 수정 후 바로 반영할 때 사용.
    주의: 현재 녹화 중인 streamlink는 종료되고, 남은 ts는 recorder_v07.py의 백그라운드 후처리로 처리됩니다.
    """
    global recorder_proc

    if send_supervisor_command("reload", "RECORDER"):
        return {
            "ok": True,
            "message": "Supervisor에 안전한 녹화기 리로드를 요청했습니다.",
            "was_running": True,
        }

    was_running = recorder_proc is not None and recorder_proc.poll() is None

    if was_running:
        stop_process(recorder_proc, timeout_sec=20)
        recorder_proc = None
        time.sleep(1.5)

    recorder_proc = start_process("recorder_v07.py")

    return {
        "ok": True,
        "message": "녹화기를 리로드했습니다.",
        "was_running": was_running,
    }


@app.post("/api/youtube/start")
def start_youtube():
    global uploader_proc

    if uploader_proc is not None and uploader_proc.poll() is None:
        return {"ok": True, "message": "이미 실행 중입니다."}

    existing = find_script_process_ids("youtube_uploader.py")
    if existing:
        # The uploader may outlive a dashboard reload.  Do not create a second
        # child merely because this new dashboard process has no Popen handle.
        return {
            "ok": True,
            "message": "이미 실행 중인 YouTube 업로더를 확인했습니다.",
            "existing_pids": existing,
        }

    # 채팅 오버레이 업로더가 있으면 우선 사용, 없으면 일반 업로더
    script = "youtube_uploader_chat_overlay.py"
    if not (BASE_DIR / script).exists():
        script = "youtube_uploader.py"

    # Console output is relayed from the persistent uploader log so it survives
    # dashboard reloads without tying upload lifetime to a fragile stdout pipe.
    uploader_proc = start_process(script)
    return {"ok": True, "message": "YouTube 업로더를 시작했습니다.", "script": script}


@app.post("/api/youtube/stop")
def stop_youtube():
    global uploader_proc

    if uploader_proc is None or uploader_proc.poll() is not None:
        uploader_proc = None
        existing = find_script_process_ids("youtube_uploader.py")
        if not existing:
            return {"ok": True, "message": "실행 중이 아닙니다."}
        # A reloaded dashboard cannot retain the old Popen handle. Terminate
        # only the specifically identified uploader PIDs; recorder processes
        # are never included in this path.
        for pid in existing:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception:
                pass
        return {"ok": True, "message": "기존 YouTube 업로더 종료 요청을 보냈습니다.", "pids": existing}

    stop_process(uploader_proc, timeout_sec=10)
    uploader_proc = None
    return {"ok": True, "message": "YouTube 업로더 종료 요청을 보냈습니다."}


@app.post("/api/open/drive")
def open_drive():
    path = load_config()["paths"]["drive_dir"]
    if os.name == "nt":
        os.startfile(path)
    return {"ok": True}


@app.post("/api/open/logs")
def open_logs():
    path = load_config()["paths"]["logs_dir"]
    if os.name == "nt":
        os.startfile(path)
    return {"ok": True}

