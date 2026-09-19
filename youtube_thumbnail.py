"""Automatic YouTube thumbnail creation and rate-limit-safe delivery.

The video upload is the primary operation.  Every public function in this
module is best-effort: thumbnail generation/API failures are persisted for a
later retry and never turn a completed video transfer into a failed upload.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload


BASE_DIR = Path(__file__).resolve().parent
_STATE_LOCK = threading.RLock()
_APPLY_LOCK = threading.Lock()
_AI_GENERATION_LOCK = threading.Lock()
_AI_KEY_MISSING_LOGGED = False


@contextmanager
def _named_mutex(name, timeout_ms=15000):
    """Serialize thumbnail state/API work across dashboard and uploader processes."""
    if __import__("os").name != "nt":
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError("thumbnail mutex creation failed")
    acquired = kernel32.WaitForSingleObject(handle, int(timeout_ms))
    if acquired not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
        kernel32.CloseHandle(handle)
        raise TimeoutError(f"thumbnail mutex timeout: {name}")
    try:
        yield
    finally:
        kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


@contextmanager
def _state_guard():
    with _STATE_LOCK:
        with _named_mutex("Local\\CHZZK_YOUTUBE_THUMBNAIL_STATE_V1"):
            yield


def _log(logger, message, icon="THUMB"):
    try:
        logger(message, icon)
    except Exception:
        # Some maintenance shells still expose a cp1252 stdout.  Logging must
        # remain best-effort even when the message contains Korean text.
        line = f"[{icon}] {message}"
        encoding = getattr(getattr(__import__("sys"), "stdout", None), "encoding", None) or "utf-8"
        print(line.encode(encoding, errors="backslashreplace").decode(encoding), flush=True)


def _settings(config):
    yt = config.get("youtube", {}) if isinstance(config, dict) else {}
    raw = yt.get("thumbnail", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "output_dir": Path(raw.get("output_dir") or (BASE_DIR / "thumbnails" / "auto")),
        "font_path": Path(raw.get("font_path") or r"C:\Windows\Fonts\malgunbd.ttf"),
        "retry_interval_sec": max(60, int(raw.get("retry_interval_sec", 600))),
        "rate_limit_retry_sec": max(3600, int(raw.get("rate_limit_retry_sec", 21600))),
        "transient_retry_sec": max(300, int(raw.get("transient_retry_sec", 3600))),
        "import_prepared_batches": bool(raw.get("import_prepared_batches", True)),
        "ai_enabled": bool(raw.get("ai_enabled", False)),
        "ai_model": str(raw.get("ai_model") or "gpt-image-2"),
        "ai_quality": str(raw.get("ai_quality") or "medium"),
        "ai_size": str(raw.get("ai_size") or "1536x864"),
        "ai_output_dir": Path(raw.get("ai_output_dir") or (BASE_DIR / "thumbnails" / "ai")),
        "ai_request_timeout_sec": max(120, int(raw.get("ai_request_timeout_sec", 420))),
        "openai_key_file": Path(
            raw.get("openai_key_file") or (BASE_DIR / "logs" / "openai_image_api_key.dpapi")
        ),
    }


def _state_path(config):
    logs_dir = Path(config.get("paths", {}).get("logs_dir") or (BASE_DIR / "logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir / "youtube_thumbnail_state.json"


def _empty_state():
    return {
        "version": 1,
        "rate_limited_until": 0,
        "pending": {},
        "planned": {},
        "completed": {},
        "failed": {},
        "updated_at": None,
    }


def _load_state(config):
    path = _state_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("thumbnail state is not an object")
    except FileNotFoundError:
        data = _empty_state()
    except Exception:
        broken = path.with_suffix(f".broken-{int(time.time())}.json")
        try:
            path.replace(broken)
        except Exception:
            pass
        data = _empty_state()
    defaults = _empty_state()
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def _save_state(config, state):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(f".tmp-{threading.get_ident()}.json")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _trim_history(mapping, limit=1000):
    if len(mapping) <= limit:
        return mapping
    ordered = sorted(
        mapping.items(),
        key=lambda item: str((item[1] or {}).get("finished_at") or ""),
        reverse=True,
    )
    return dict(ordered[:limit])


def _ffmpeg_path(config):
    yt = config.get("youtube", {})
    candidates = [
        yt.get("ffmpeg_path"),
        BASE_DIR / "tools" / "ffmpeg.exe",
        BASE_DIR / "ffmpeg.exe",
        "ffmpeg",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        if str(candidate) == "ffmpeg" or Path(candidate).exists():
            return str(candidate)
    return "ffmpeg"


def _duration_seconds(config, source):
    try:
        proc = subprocess.run(
            [_ffmpeg_path(config), "-hide_banner", "-i", str(source)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        text = (proc.stdout or "") + "\n" + (proc.stderr or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    except Exception:
        pass
    return None


def _clean_text(value):
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) != 127).strip()


def _wrap_title(value, width=19, max_lines=2):
    text = _clean_text(value) or "방송 다시보기"
    # The upload title often ends with "| streamer | date".  The badge already
    # shows the streamer, so keep the broadcast part large and readable.
    text = text.split(" | ", 1)[0].strip() or text
    lines = []
    rest = text
    while rest and len(lines) < max_lines:
        if len(rest) <= width:
            lines.append(rest)
            rest = ""
            break
        cut = rest.rfind(" ", 0, width + 1)
        if cut < max(5, width // 2):
            cut = width
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest and lines:
        lines[-1] = (lines[-1][: max(1, width - 1)].rstrip() + "…")
    return "\n".join(lines[:max_lines])


def _headline_text(value, streamer=""):
    """Turn a recorder title into one short, poster-style headline."""
    text = _clean_text(value) or "방송 다시보기"
    text = text.split(" | ", 1)[0].strip()
    text = re.sub(r"^\s*\[[^\]]{1,30}\]\s*", "", text)
    parts = [part.strip(" /·|-") for part in re.split(r"\s*/\s*", text) if part.strip(" /·|-")]
    if parts:
        # Prefer a descriptive phrase over greetings or one-character fragments.
        text = max(parts[:4], key=lambda part: (min(len(part), 24), len(part) >= 6))
    if streamer:
        text = re.sub(re.escape(str(streamer)), "", text, flags=re.I).strip(" -|/") or text
    text = re.sub(r"([!?~ㅋㅎㅠㅜ])\1{2,}", r"\1\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:21].rstrip() + "…") if len(text) > 22 else text


def _ai_prompt(row, upload_title):
    streamer = _clean_text(row.get("streamer_name")) or "RECORDER"
    headline = _headline_text(row.get("broadcast_title") or upload_title, streamer)
    return f"""Use case: ads-marketing
Asset type: premium YouTube livestream archive thumbnail, 16:9
Primary request: Transform the supplied broadcast frame into a polished, high-click custom thumbnail. Recompose around the most recognizable game, avatar, person, or event in the reference. Remove browser chrome, webpage navigation, chat panels, tiny UI clutter, dates, and every old lower-third or FULL STREAM template. Create a dramatic focal subject, cinematic depth, saturated but natural color, rim lighting, impact glow, and tasteful motion accents. The result must look designed by a professional gaming thumbnail artist, not like a screenshot with a caption.
Text (verbatim): "{headline}"
Small badge text (verbatim): "{streamer}"
Composition/framing: very large Korean headline with thick black outline and strong white/yellow or scene-matched color hierarchy; readable at mobile size; one clear focal subject; uncluttered background.
Constraints: preserve recognizable visual cues from the reference; exact Korean text; no extra words; no date; no watermark; no browser frame; no chat UI; no tiny unreadable text; no FULL STREAM label."""


def create_ai_thumbnail(config, row, reference_path, upload_title, logger, variant=0, force=False):
    """Create one GPT-Image thumbnail, falling back silently to the local candidate."""
    global _AI_KEY_MISSING_LOGGED
    settings = _settings(config)
    if not settings["ai_enabled"]:
        return None
    reference = Path(str(reference_path or ""))
    if not reference.is_file():
        return None
    try:
        from openai_image_api import edit_image, load_api_key

        api_key = load_api_key(settings["openai_key_file"])
        if not api_key:
            if not _AI_KEY_MISSING_LOGGED:
                _log(logger, "AI 썸네일 대기: OPENAI_IMAGE_API_SETUP.cmd에서 API 키를 먼저 저장하세요.", "THUMB-AI")
                _AI_KEY_MISSING_LOGGED = True
            return None
        output_dir = settings["ai_output_dir"]
        output_dir.mkdir(parents=True, exist_ok=True)
        rec_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(row.get("id") or "unknown"))
        output = output_dir / f"recording-{rec_id}-v{int(variant)}.jpg"
        if not force and output.is_file() and output.stat().st_size >= 30_000:
            return output
        _log(
            logger,
            f"AI 고품질 썸네일 생성 시작 #{row.get('id')} [{row.get('streamer_name')}] / 후보 {int(variant) + 1}",
            "THUMB-AI",
        )
        # One generation at a time prevents two uploader workers from doubling
        # spend and keeps image traffic away from live recording bursts.
        with _AI_GENERATION_LOCK:
            with _named_mutex("Local\\CHZZK_OPENAI_THUMBNAIL_GENERATION_V1", timeout_ms=480000):
                edit_image(
                    api_key,
                    reference,
                    output,
                    _ai_prompt(row, upload_title),
                    model=settings["ai_model"],
                    quality=settings["ai_quality"],
                    size=settings["ai_size"],
                    output_format="jpeg",
                    output_compression=86,
                    timeout=settings["ai_request_timeout_sec"],
                )
        if output.is_file() and output.stat().st_size >= 30_000:
            _log(logger, f"AI 고품질 썸네일 생성 완료 #{row.get('id')} / {output.name}", "THUMB-AI")
            return output
        return None
    except BaseException as exc:
        _log(
            logger,
            f"AI 썸네일 실패 → 로컬 후보로 안전 대체 #{row.get('id')}: {type(exc).__name__}: {exc}",
            "THUMB-AI",
        )
        return None


def _filter_path(path):
    # ffmpeg filter arguments have their own parser.  Forward slashes plus an
    # escaped drive colon work reliably even though subprocess does no shell parsing.
    return str(Path(path).resolve()).replace("\\", "/").replace(":", r"\:")


def _candidate_timestamps(duration, variant=0):
    if not duration or duration < 8:
        return [0]
    end = max(1.0, duration - 3.0)
    values = [duration * 0.35, duration * 0.18, duration * 0.58, 30.0]
    result = []
    for value in values:
        value = max(1.0, min(end, value))
        if all(abs(value - old) > 1 for old in result):
            result.append(value)
    if result:
        shift = int(variant or 0) % len(result)
        result = result[shift:] + result[:shift]
    return result


def create_thumbnail(config, row, video_id, upload_title, logger, variant=0, force=False):
    """Create a 1280x720 JPEG from a representative frame."""
    settings = _settings(config)
    if not settings["enabled"]:
        return None
    source = Path(str(row.get("final_path") or ""))
    if not source.is_file():
        _log(logger, f"썸네일 생성 건너뜀 #{row.get('id')}: 원본 없음 {source}", "THUMB")
        return None
    font = settings["font_path"]
    if not font.is_file():
        _log(logger, f"썸네일 생성 건너뜀: 한글 글꼴 없음 {font}", "THUMB")
        return None

    output_dir = settings["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(video_id or row.get("id") or int(time.time())))
    output = output_dir / f"{safe_id}.jpg"
    if not force and output.is_file() and output.stat().st_size >= 20_000:
        return output

    meta_dir = output_dir / ".text"
    meta_dir.mkdir(parents=True, exist_ok=True)
    title_file = meta_dir / f"{safe_id}-title.txt"
    badge_file = meta_dir / f"{safe_id}-badge.txt"
    streamer = _clean_text(row.get("streamer_name")) or "RECORDER"
    title_file.write_text(_wrap_title(row.get("broadcast_title") or upload_title), encoding="utf-8")
    badge_file.write_text(f"{streamer}  ·  FULL STREAM", encoding="utf-8")

    font_filter = _filter_path(font)
    title_filter = _filter_path(title_file)
    badge_filter = _filter_path(badge_file)
    vf = ",".join(
        [
            "scale=1280:720:force_original_aspect_ratio=increase",
            "crop=1280:720",
            "eq=contrast=1.04:saturation=1.12:brightness=-0.025",
            "drawbox=x=0:y=495:w=iw:h=225:color=black@0.72:t=fill",
            "drawbox=x=0:y=0:w=iw:h=9:color=0x55e6c1@1:t=fill",
            f"drawtext=fontfile='{font_filter}':textfile='{badge_filter}':fontcolor=0x70f3d0:fontsize=32:x=64:y=520",
            f"drawtext=fontfile='{font_filter}':textfile='{title_filter}':fontcolor=white:fontsize=50:line_spacing=10:x=64:y=570:shadowcolor=black@0.95:shadowx=3:shadowy=3",
        ]
    )

    duration = _duration_seconds(config, source)
    last_error = ""
    try:
        for timestamp in _candidate_timestamps(duration, variant=variant):
            output.unlink(missing_ok=True)
            cmd = [
                _ffmpeg_path(config), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{timestamp:.3f}", "-i", str(source),
                "-vf", vf, "-frames:v", "1", "-q:v", "3", str(output),
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=180,
                )
                last_error = (proc.stderr or proc.stdout or "").strip()[-1200:]
                if proc.returncode == 0 and output.is_file() and output.stat().st_size >= 20_000:
                    _log(
                        logger,
                        f"자동 썸네일 생성 #{row.get('id')} [{streamer}] / {timestamp:.0f}초 / {output.name}",
                        "THUMB",
                    )
                    return output
            except Exception as exc:
                last_error = str(exc)
    finally:
        title_file.unlink(missing_ok=True)
        badge_file.unlink(missing_ok=True)

    output.unlink(missing_ok=True)
    _log(logger, f"자동 썸네일 생성 실패 #{row.get('id')}: {last_error or 'unknown error'}", "THUMB")
    return None


def _error_details(exc):
    status = int(getattr(getattr(exc, "resp", None), "status", 0) or 0)
    reason = ""
    message = str(exc)
    content = getattr(exc, "content", None)
    try:
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        payload = json.loads(content or "{}")
        errors = ((payload.get("error") or {}).get("errors") or [])
        if errors:
            reason = str(errors[0].get("reason") or "")
        message = str((payload.get("error") or {}).get("message") or message)
    except Exception:
        pass
    return status, reason, message


def _queue(config, video_id, thumbnail_path, row, error, next_retry_at, attempts=0):
    with _state_guard():
        state = _load_state(config)
        current = (state.get("pending") or {}).get(str(video_id), {})
        state["pending"][str(video_id)] = {
            "video_id": str(video_id),
            "recording_id": row.get("id") if isinstance(row, dict) else current.get("recording_id"),
            "streamer_name": (row.get("streamer_name") if isinstance(row, dict) else None) or current.get("streamer_name"),
            "thumbnail_path": str(thumbnail_path),
            "attempts": max(int(current.get("attempts") or 0), int(attempts or 0)),
            "next_retry_at": float(next_retry_at),
            "last_error": str(error or "")[:1500],
            "queued_at": current.get("queued_at") or datetime.now(timezone.utc).isoformat(),
        }
        _save_state(config, state)


def apply_or_queue_thumbnail(
    config, service, row, video_id, thumbnail_path, logger, force_apply=False
):
    """Apply once; persist any failure without raising into the video uploader."""
    if not video_id or not thumbnail_path:
        return False
    settings = _settings(config)
    path = Path(thumbnail_path)
    if not path.is_file():
        return False

    with _state_guard():
        state = _load_state(config)
        if not force_apply and str(video_id) in (state.get("completed") or {}):
            return True
        blocked_until = float(state.get("rate_limited_until") or 0)
    if blocked_until > time.time():
        _queue(config, video_id, path, row, "thumbnail rate limit cooldown", blocked_until)
        _log(logger, f"썸네일 적용 보류 {video_id}: 제한 해제 대기 큐에 안전하게 저장", "THUMB")
        return False

    try:
        # Serialize only API calls, not video transfers.  This prevents the two
        # uploader workers from spending two thumbnail requests at the same instant.
        with _APPLY_LOCK:
            with _named_mutex("Local\\CHZZK_YOUTUBE_THUMBNAIL_API_V1"):
                service.thumbnails().set(
                    videoId=str(video_id),
                    media_body=MediaFileUpload(str(path), mimetype="image/jpeg", resumable=False),
                ).execute()
        with _state_guard():
            state = _load_state(config)
            state["pending"].pop(str(video_id), None)
            state["failed"].pop(str(video_id), None)
            state["completed"][str(video_id)] = {
                "thumbnail_path": str(path),
                "recording_id": row.get("id") if isinstance(row, dict) else None,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            state["completed"] = _trim_history(state["completed"])
            _save_state(config, state)
        _log(logger, f"YouTube 썸네일 적용 완료 {video_id} / {path.name}", "THUMB")
        return True
    except Exception as exc:
        status, reason, message = _error_details(exc)
        now_ts = time.time()
        rate_limited = reason in {"uploadRateLimitExceeded", "rateLimitExceeded"} or (
            status == 429 and "thumbnail" in f"{reason} {message}".lower()
        )
        delay = settings["rate_limit_retry_sec"] if rate_limited else settings["transient_retry_sec"]
        with _state_guard():
            state = _load_state(config)
            old = (state.get("pending") or {}).get(str(video_id), {})
            attempts = int(old.get("attempts") or 0) + 1
            # Back off repeated rate-limit hits up to one day.
            if rate_limited:
                delay = min(86400, delay * (2 ** min(2, max(0, attempts - 1))))
                state["rate_limited_until"] = max(float(state.get("rate_limited_until") or 0), now_ts + delay)
                _save_state(config, state)
        _queue(
            config,
            video_id,
            path,
            row,
            f"HTTP {status} {reason}: {message}",
            now_ts + delay,
            attempts=attempts,
        )
        label = "최근 썸네일 변경 한도" if rate_limited else "API 오류"
        _log(logger, f"썸네일 {label} → 영상은 완료 유지, {int(delay / 3600)}시간 후 재시도 {video_id}", "THUMB")
        return False


def prepare_thumbnail_variants(config, row, upload_title, logger, select_next=False):
    """Prepare three scene choices and upgrade the selected one with GPT Image."""
    try:
        rec_id = str(row.get("id") or "").strip()
        if not rec_id:
            return None
        with _state_guard():
            state = _load_state(config)
            previous = (state.get("planned") or {}).get(rec_id, {})
        previous_title = str(previous.get("upload_title") or "")
        active_variant = int(previous.get("active_variant") or 0)
        if select_next:
            active_variant = (active_variant + 1) % 3
        title_changed = previous_title != str(upload_title or "")

        source_paths = []
        for variant in range(3):
            key = f"recording-{rec_id}-v{variant}"
            path = create_thumbnail(
                config,
                row,
                key,
                upload_title,
                logger,
                variant=variant,
                force=title_changed,
            )
            if path:
                source_paths.append(str(path))
            else:
                old_paths = previous.get("source_variant_paths") or previous.get("variant_paths") or []
                if variant < len(old_paths) and Path(old_paths[variant]).is_file():
                    source_paths.append(str(old_paths[variant]))
                else:
                    source_paths.append("")
        if not any(source_paths):
            return None
        if not source_paths[active_variant]:
            active_variant = next(i for i, path in enumerate(source_paths) if path)

        # Preserve already-paid AI candidates while keeping local source frames
        # separately.  A dashboard "different scene" click creates at most one
        # new AI image for the newly selected variant.
        old_selected_paths = list(previous.get("variant_paths") or [])
        paths = []
        for variant, source_path in enumerate(source_paths):
            old = old_selected_paths[variant] if variant < len(old_selected_paths) else ""
            paths.append(old if old and Path(old).is_file() else source_path)
        ai_path = create_ai_thumbnail(
            config,
            row,
            source_paths[active_variant],
            upload_title,
            logger,
            variant=active_variant,
            force=bool(title_changed),
        )
        if ai_path:
            paths[active_variant] = str(ai_path)
        selected = paths[active_variant]
        with _state_guard():
            state = _load_state(config)
            state["planned"][rec_id] = {
                "recording_id": row.get("id"),
                "streamer_name": row.get("streamer_name"),
                "broadcast_title": row.get("broadcast_title"),
                "upload_title": upload_title,
                "source_path": str(row.get("final_path") or ""),
                "source_variant_paths": source_paths,
                "variant_paths": paths,
                "active_variant": active_variant,
                "thumbnail_path": selected,
                "thumbnail_kind": "ai" if Path(selected).parent == _settings(config)["ai_output_dir"] else "local",
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            _save_state(config, state)
        if select_next:
            _log(
                logger,
                f"썸네일 후보 변경 #{rec_id}: {active_variant + 1}/3 / {Path(selected).name}",
                "THUMB",
            )
        return Path(selected)
    except BaseException as exc:
        _log(logger, f"썸네일 후보 준비 예외 #{row.get('id')}: {type(exc).__name__}: {exc}", "THUMB")
        return None


def prepare_and_apply_thumbnail(config, service, row, video_id, upload_title, logger):
    """Select the planned image (or create it) and apply without propagating."""
    try:
        path = prepare_thumbnail_variants(config, row, upload_title, logger)
        if not path:
            return False
        return apply_or_queue_thumbnail(config, service, row, video_id, path, logger)
    except BaseException as exc:
        _log(logger, f"썸네일 처리 예외 격리 #{row.get('id')}: {type(exc).__name__}: {exc}", "THUMB")
        return False


def import_prepared_batch_thumbnails(config, logger):
    """Adopt previously generated, rate-limited thumbnails into the retry queue."""
    if not _settings(config)["import_prepared_batches"]:
        return 0
    imported = 0
    now_ts = time.time()
    with _state_guard():
        state = _load_state(config)
        for manifest in sorted((BASE_DIR / "thumbnails").glob("thumbnail_batch_*.json")):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            for item in payload.get("items") or []:
                video_id = str(item.get("video_id") or "").strip()
                status = str(item.get("status") or "")
                if not video_id or not status.startswith("prepared"):
                    continue
                if video_id in state["completed"] or video_id in state["pending"]:
                    continue
                image = Path(str(item.get("file") or ""))
                if not image.is_absolute():
                    image = manifest.parent / image
                if not image.is_file():
                    continue
                state["pending"][video_id] = {
                    "video_id": video_id,
                    "recording_id": None,
                    "streamer_name": item.get("streamer"),
                    "thumbnail_path": str(image),
                    "attempts": 0,
                    "next_retry_at": now_ts,
                    "last_error": item.get("error") or "imported prepared thumbnail",
                    "queued_at": datetime.now(timezone.utc).isoformat(),
                }
                imported += 1
        if imported:
            _save_state(config, state)
    if imported:
        _log(logger, f"기존 제한 보류 썸네일 {imported}개를 자동 재시도 큐로 승계", "THUMB")
    return imported


def retry_pending_once(config, service, logger):
    """Retry at most one image per poll to avoid another thumbnail burst limit."""
    now_ts = time.time()
    with _state_guard():
        state = _load_state(config)
        if float(state.get("rate_limited_until") or 0) > now_ts:
            return False
        due = [
            item for item in (state.get("pending") or {}).values()
            if float((item or {}).get("next_retry_at") or 0) <= now_ts
        ]
    if not due:
        return False
    due.sort(key=lambda item: (float(item.get("next_retry_at") or 0), str(item.get("queued_at") or "")))
    item = due[0]
    path = Path(str(item.get("thumbnail_path") or ""))
    video_id = str(item.get("video_id") or "")
    if not path.is_file():
        with _state_guard():
            state = _load_state(config)
            state["pending"].pop(video_id, None)
            state["failed"][video_id] = {
                **item,
                "last_error": "thumbnail file missing",
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            state["failed"] = _trim_history(state["failed"])
            _save_state(config, state)
        _log(logger, f"썸네일 재시도 제외 {video_id}: 파일 없음 {path}", "THUMB")
        return False
    row = {"id": item.get("recording_id"), "streamer_name": item.get("streamer_name")}
    _log(logger, f"보류 썸네일 재시도 {video_id} / 남은 큐 {len(due)}개", "THUMB")
    return apply_or_queue_thumbnail(config, service, row, video_id, path, logger)


def start_thumbnail_retry_thread(config, service_factory, logger):
    settings = _settings(config)
    if not settings["enabled"] or getattr(start_thumbnail_retry_thread, "_started", False):
        return
    start_thumbnail_retry_thread._started = True
    import_prepared_batch_thumbnails(config, logger)

    def loop():
        service = None
        # Let uploader clients finish construction and avoid an API burst at boot.
        time.sleep(20)
        _log(logger, "자동 썸네일 재시도 스레드 시작", "THUMB")
        while True:
            try:
                if service is None:
                    service = service_factory(config)
                retry_pending_once(config, service, logger)
            except BaseException as exc:
                service = None
                _log(logger, f"썸네일 재시도 루프 복구: {type(exc).__name__}: {exc}", "THUMB")
            time.sleep(settings["retry_interval_sec"])

    threading.Thread(target=loop, name="youtube-thumbnail-retry", daemon=True).start()
