from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
import websocket


ROOT = Path(__file__).resolve().parent
_chrome_candidates = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Google" / "Chrome" / "Application" / "chrome.exe",
]
CHROME = next((candidate for candidate in _chrome_candidates if candidate.is_file()), _chrome_candidates[0])
PROFILE = ROOT / ".soop_chrome_profile"
CDP_PORT = 9225
CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"
FFMPEG = ROOT / "tools" / "ffmpeg.exe"
SOOP_PACKAGE = Path.home() / "AppData" / "Local" / "SOOP" / "SOOPPackage.exe"
SOOP_AGENT_PORT = 21201
SOOP_LIVE_API_URLS = (
    "https://live.sooplive.com/afreeca/player_live_api.php",
    "https://live.sooplive.co.kr/afreeca/player_live_api.php",
)
STOP_REQUESTED = False


def media_operation_timeout(path: Path, minimum: int = 600,
                            seconds_per_gib: int = 120,
                            maximum: int = 21600) -> int:
    """Give very large local recordings enough time while still bounding hangs."""
    try:
        size_gib = max(0.0, path.stat().st_size / (1024 ** 3))
    except OSError:
        size_gib = 0.0
    return int(max(minimum, min(maximum, size_gib * seconds_per_gib)))


def acquire_recovery_mutex(path: Path):
    if os.name != "nt":
        return None, True
    digest = hashlib.sha1(str(path.resolve()).casefold().encode("utf-8")).hexdigest()[:20]
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\CHZZK_SOOP_RECOVERY_{digest}")
    if not handle:
        return None, False
    wait_result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
    if wait_result not in (0x00000000, 0x00000080):
        ctypes.windll.kernel32.CloseHandle(handle)
        return None, False
    return handle, True


def release_recovery_mutex(handle) -> None:
    if not handle or os.name != "nt":
        return
    ctypes.windll.kernel32.ReleaseMutex(handle)
    ctypes.windll.kernel32.CloseHandle(handle)


def log(message: str) -> None:
    print(f"[SOOP_AGENT] {message}", flush=True)


def request_stop(signum=None, frame=None) -> None:  # noqa: ARG001
    global STOP_REQUESTED
    STOP_REQUESTED = True


class CDP:
    def __init__(self, websocket_url: str) -> None:
        self.ws = websocket.create_connection(
            websocket_url,
            timeout=3,
            suppress_origin=True,
        )
        self.ids = itertools.count(1)

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = next(self.ids)
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def cdp_available() -> bool:
    try:
        requests.get(f"{CDP_HTTP}/json/version", timeout=2).raise_for_status()
        return True
    except Exception:
        return False


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False



def channel_id_from_url(url: str) -> str:
    """Return the SOOP broadcaster id from a play/channel URL."""
    try:
        return next((part for part in urlparse(url).path.split("/") if part), "")
    except Exception:
        return ""


def probe_live_api(channel_id: str, timeout: int = 5) -> dict | None:
    """Use SOOP's current player API as the first live/offline signal.

    The player object's internal JavaScript fields change relatively often.
    CHANNEL.RESULT/BNO is the same public signal the current web player and
    third-party clients use, so it is a much more stable probe.  Restricted
    rooms can return non-standard result codes; those remain "unknown" and
    fall back to the signed-in browser probe instead of being forced offline.
    """
    if not channel_id:
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://play.sooplive.com",
        "Referer": f"https://play.sooplive.com/{channel_id}",
    }
    data = {
        "bid": channel_id,
        "bno": "",
        "type": "live",
        "pwd": "",
        "player_type": "html5",
        "stream_type": "common",
        "mode": "landing",
        "from_api": "0",
    }

    last_error = ""
    definite_offline: dict | None = None
    for endpoint in SOOP_LIVE_API_URLS:
        try:
            response = requests.post(endpoint, data=data, headers=headers, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            channel = payload.get("CHANNEL") if isinstance(payload, dict) else None
            if not isinstance(channel, dict):
                last_error = f"{endpoint}: CHANNEL missing"
                continue

            raw_result = channel.get("RESULT")
            try:
                result_code = int(raw_result)
            except (TypeError, ValueError):
                last_error = f"{endpoint}: invalid RESULT={raw_result!r}"
                continue

            title = str(channel.get("TITLE") or "")
            broadcast_id = str(channel.get("BNO") or "")
            if result_code == 1 and broadcast_id:
                return {
                    "ok": True,
                    "check_ok": True,
                    "is_live": True,
                    "title": title,
                    "broadcast_id": broadcast_id,
                    "live_reason": "player_live_api",
                    "api_result": result_code,
                }

            # RESULT 0 / -1 are ordinary "not live / no broadcast" responses.
            # Other negative codes can mean login/password/adult restrictions,
            # so do not misclassify those as offline.
            if result_code in (0, -1):
                definite_offline = {
                    "ok": True,
                    "check_ok": True,
                    "is_live": False,
                    "title": title,
                    "broadcast_id": broadcast_id,
                    "offline_reason": "player_live_api",
                    "api_result": result_code,
                }
            else:
                last_error = (
                    f"{endpoint}: RESULT={result_code}, "
                    f"BNO={broadcast_id or '-'}"
                )
        except Exception as exc:
            last_error = f"{endpoint}: {type(exc).__name__}: {exc}"

    if definite_offline:
        return definite_offline
    if last_error:
        log(f"SOOP live API inconclusive for {channel_id}: {last_error}")
    return None


def ensure_soop_agent() -> None:
    """Start SOOP's official local agent when Windows did not restore it."""
    if port_open(SOOP_AGENT_PORT):
        return
    if not SOOP_PACKAGE.exists():
        raise FileNotFoundError(f"SOOPPackage not found: {SOOP_PACKAGE}")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(
        [str(SOOP_PACKAGE)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if port_open(SOOP_AGENT_PORT):
            return
        time.sleep(0.5)
    raise RuntimeError("SOOP official agent did not open port 21201")


def ensure_browser() -> None:
    if cdp_available():
        return
    if not CHROME.exists():
        raise FileNotFoundError(f"Chrome not found: {CHROME}")
    PROFILE.mkdir(parents=True, exist_ok=True)
    args = [
        str(CHROME),
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={PROFILE}",
        "--no-first-run",
        "--no-default-browser-check",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "about:blank",
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if cdp_available():
            return
        time.sleep(0.5)
    raise RuntimeError("SOOP dedicated Chrome did not open CDP port 9225")


def create_target() -> dict:
    encoded = quote("about:blank", safe="")
    response = requests.put(f"{CDP_HTTP}/json/new?{encoded}", timeout=5)
    response.raise_for_status()
    return response.json()


def close_target(target_id: str) -> None:
    try:
        requests.get(f"{CDP_HTTP}/json/close/{target_id}", timeout=3)
    except Exception:
        pass


def release_target(cdp: CDP, target_id: str) -> None:
    """Leave one minimized blank page alive so later parts do not pop Chrome again."""
    try:
        targets = requests.get(f"{CDP_HTTP}/json/list", timeout=3).json()
        pages = [item for item in targets if item.get("type") == "page"]
        if len(pages) <= 1:
            cdp.call("Page.navigate", {"url": "about:blank"})
            minimize_target_window(cdp, target_id)
            return
    except Exception:
        pass
    close_target(target_id)


def minimize_target_window(cdp: CDP, target_id: str) -> None:
    try:
        result = cdp.call("Browser.getWindowForTarget", {"targetId": target_id})
        window_id = result.get("windowId")
        if window_id is not None:
            cdp.call(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "minimized"}},
            )
    except Exception as exc:
        log(f"could not minimize dedicated browser: {exc}")


def nal_types(media: bytes) -> set[int]:
    """Return H.264 NAL types from 3-byte or 4-byte Annex-B start codes."""
    found: set[int] = set()
    cursor = 0
    length = len(media)
    while cursor + 4 <= length:
        if media[cursor:cursor + 4] == b"\x00\x00\x00\x01":
            header = cursor + 4
            if header < length:
                found.add(media[header] & 0x1F)
            cursor = header + 1
            continue
        if media[cursor:cursor + 3] == b"\x00\x00\x01":
            header = cursor + 3
            if header < length:
                found.add(media[header] & 0x1F)
            cursor = header + 1
            continue
        cursor += 1
    return found


def _valid_adts_at(payload: bytes, index: int) -> bool:
    if index < 0 or index + 7 > len(payload):
        return False
    if payload[index] != 0xFF or (payload[index + 1] & 0xF6) != 0xF0:
        return False
    frequency_index = (payload[index + 2] >> 2) & 0x0F
    if frequency_index >= 13:
        return False
    frame_length = (
        ((payload[index + 3] & 0x03) << 11)
        | (payload[index + 4] << 3)
        | ((payload[index + 5] >> 5) & 0x07)
    )
    return frame_length >= 7 and index + frame_length <= len(payload)


def unpack_agent_media(payload: bytes) -> tuple[str | None, bytes, int]:
    """Extract H.264/AAC from a SOOP agent WebSocket packet.

    Older SOOP Package versions used an 8xFF marker and a fixed 77-byte
    envelope.  Newer player/package builds can change the envelope while the
    elementary media itself is still Annex-B H.264 or ADTS AAC.  Keep the old
    fast path, then scan only the small packet prefix for validated media sync
    markers so an envelope-size change does not silently stop recording.
    """
    if not payload:
        return None, b"", 0

    # Legacy format first.
    if len(payload) > 77 and payload[:8] == b"\xff" * 8:
        media = payload[77:]
        if (
            media.startswith(b"\x00\x00\x00\x01")
            or media.startswith(b"\x00\x00\x01")
        ):
            return "video", media, 77
        if _valid_adts_at(media, 0):
            return "audio", media, 77

    scan_limit = min(len(payload), 512)

    # Find a plausible Annex-B H.264 start code. NAL type 1..12 covers normal
    # slices, IDR, SEI, SPS/PPS and access-unit delimiters while rejecting most
    # random metadata matches.
    video_index: int | None = None
    for index in range(max(0, scan_limit - 4)):
        start_len = 0
        if payload[index:index + 4] == b"\x00\x00\x00\x01":
            start_len = 4
        elif payload[index:index + 3] == b"\x00\x00\x01":
            start_len = 3
        if not start_len or index + start_len >= len(payload):
            continue
        nal_type = payload[index + start_len] & 0x1F
        if 1 <= nal_type <= 12:
            video_index = index
            break

    audio_index: int | None = None
    for index in range(max(0, scan_limit - 7)):
        if _valid_adts_at(payload, index):
            audio_index = index
            break

    if video_index is not None and (
        audio_index is None or video_index <= audio_index
    ):
        return "video", payload[video_index:], video_index
    if audio_index is not None:
        return "audio", payload[audio_index:], audio_index
    return None, b"", 0


def choose_fps(frame_count: int, elapsed: float) -> int:
    if frame_count <= 0 or elapsed <= 0:
        return 60
    observed = frame_count / elapsed
    candidates = (24, 25, 30, 50, 60, 120)
    return min(candidates, key=lambda value: abs(value - observed))


def sync_fps(frame_count: int, duration: float, fallback: int = 60) -> float:
    """Return the exact CFR needed to keep raw H.264 aligned with audio.

    The SOOP agent delivers elementary video and audio separately.  Rounding
    the measured video rate to 50/60 fps changes the video timeline whenever
    packets were delayed or missed, which accumulates into visible A/V drift
    on long recordings.  Preserve the measured rate instead.
    """
    if frame_count <= 0 or duration <= 0:
        return float(fallback)
    measured = frame_count / duration
    if not 10.0 <= measured <= 130.0:
        return float(fallback)
    return round(measured, 6)


def count_h264_frames(path: Path) -> int:
    """Count decoded access units, not CDP WebSocket batches.

    SOOP can bundle several 1440p frames into one agent message. Counting the
    messages therefore reports roughly 10 fps for a real 60 fps stream.
    FFmpeg's H.264 parser counts the actual access units without transcoding.
    """
    if not path.exists() or path.stat().st_size <= 0:
        return 0
    ffmpeg = str(FFMPEG if FFMPEG.exists() else (shutil.which("ffmpeg") or "ffmpeg"))
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-f",
                "h264",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "null",
                null_target,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=media_operation_timeout(path),
        )
    except Exception as exc:
        log(f"could not count H.264 frames; using timing fallback: {exc}")
        return 0
    import re

    matches = re.findall(r"frame=\s*(\d+)", (result.stdout or "") + "\n" + (result.stderr or ""))
    return int(matches[-1]) if matches else 0


def media_duration(path: Path) -> float | None:
    ffmpeg = str(FFMPEG if FFMPEG.exists() else (shutil.which("ffmpeg") or "ffmpeg"))
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=90,
        )
    except Exception:
        return None
    import re

    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr or "")
    if not match:
        return None
    return int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))


def adts_duration(path: Path) -> float | None:
    """Calculate raw AAC duration from ADTS frames instead of bitrate guesses.

    FFmpeg has no timestamp index for a standalone ADTS file and can report a
    noticeably wrong estimated duration.  Every ADTS raw-data block represents
    1024 samples, so parsing the headers gives the clock used for A/V sync.
    """
    sample_rates = (
        96000, 88200, 64000, 48000, 44100, 32000, 24000,
        22050, 16000, 12000, 11025, 8000, 7350,
    )
    if not path.exists() or path.stat().st_size < 7:
        return None
    total_samples = 0
    sample_rate: int | None = None
    try:
        data = path.read_bytes()
    except Exception:
        return None
    cursor = 0
    length = len(data)
    while cursor + 7 <= length:
        if data[cursor] != 0xFF or (data[cursor + 1] & 0xF6) != 0xF0:
            cursor += 1
            continue
        frequency_index = (data[cursor + 2] >> 2) & 0x0F
        if frequency_index >= len(sample_rates):
            cursor += 1
            continue
        frame_length = (
            ((data[cursor + 3] & 0x03) << 11)
            | (data[cursor + 4] << 3)
            | ((data[cursor + 5] >> 5) & 0x07)
        )
        if frame_length < 7 or cursor + frame_length > length:
            cursor += 1
            continue
        current_rate = sample_rates[frequency_index]
        if sample_rate is None:
            sample_rate = current_rate
        elif sample_rate != current_rate:
            log(
                f"AAC sample rate changed ({sample_rate} -> {current_rate}); "
                "using the first clock for duration"
            )
        raw_data_blocks = (data[cursor + 6] & 0x03) + 1
        total_samples += 1024 * raw_data_blocks
        cursor += frame_length
    if not sample_rate or total_samples <= 0:
        return None
    return total_samples / sample_rate


def packet_scan_ok(path: Path) -> bool:
    """Fast demux scan used before raw recovery sources are removed."""
    ffmpeg = str(FFMPEG if FFMPEG.exists() else (shutil.which("ffmpeg") or "ffmpeg"))
    null_target = "NUL" if os.name == "nt" else "/dev/null"
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-f",
                "null",
                null_target,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=media_operation_timeout(path, minimum=900, seconds_per_gib=150),
        )
    except Exception as exc:
        log(f"mux packet verification failed to run; raw sources preserved: {exc}")
        return False
    if result.returncode != 0:
        log(f"mux packet verification failed; raw sources preserved: {(result.stderr or '')[-1200:]}")
        return False
    return True


def mux_elementary(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    fps: int | float,
    duration: float,
    delete_sources: bool = True,
) -> bool:
    if not video_path.exists() or video_path.stat().st_size < 256 * 1024:
        log(f"video source too small; preserving raw file: {video_path}")
        return False
    if not audio_path.exists() or audio_path.stat().st_size < 8 * 1024:
        log(f"audio source too small; preserving raw file: {audio_path}")
        return False
    actual_frames = count_h264_frames(video_path)
    if actual_frames > 0 and duration > 0:
        nominal_fps = choose_fps(actual_frames, duration)
        fps = sync_fps(actual_frames, duration, nominal_fps)
        coverage = (float(fps) / nominal_fps * 100.0) if nominal_fps else 100.0
        log(
            f"sync timing from H.264 access units: {actual_frames} frames / "
            f"{duration:.2f}s = {fps:.6f}fps (nominal {nominal_fps}, coverage {coverage:.2f}%)"
        )
    ffmpeg = str(FFMPEG if FFMPEG.exists() else (shutil.which("ffmpeg") or "ffmpeg"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_mp4 = Path(str(output_path) + ".muxing.mp4")
    temp_output = Path(str(output_path) + ".muxing.ts")
    temp_mp4.unlink(missing_ok=True)
    temp_output.unlink(missing_ok=True)
    safe_duration = max(1.0, duration)
    make_mp4 = [
        ffmpeg,
        "-y",
        "-fflags",
        "+genpts",
        "-f",
        "h264",
        "-r",
        str(fps),
        "-i",
        str(video_path),
        "-f",
        "aac",
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-t",
        f"{safe_duration:.3f}",
        "-movflags",
        "+faststart",
        str(temp_mp4),
    ]
    result = subprocess.run(
        make_mp4,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=max(
            media_operation_timeout(video_path, minimum=1800, seconds_per_gib=180),
            min(21600, int(safe_duration * 0.5)),
        ),
    )
    if result.returncode != 0 or not temp_mp4.exists() or temp_mp4.stat().st_size < 256 * 1024:
        log(f"FFmpeg MP4 staging failed (code={result.returncode}): {(result.stderr or '')[-1200:]}")
        temp_mp4.unlink(missing_ok=True)
        temp_output.unlink(missing_ok=True)
        return False
    make_ts = [
        ffmpeg,
        "-y",
        "-i",
        str(temp_mp4),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c",
        "copy",
        "-f",
        "mpegts",
        str(temp_output),
    ]
    result = subprocess.run(
        make_ts,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=media_operation_timeout(temp_mp4, minimum=1800, seconds_per_gib=180),
    )
    temp_mp4.unlink(missing_ok=True)
    if result.returncode != 0 or not temp_output.exists() or temp_output.stat().st_size < 256 * 1024:
        log(f"FFmpeg TS staging failed (code={result.returncode}): {(result.stderr or '')[-1200:]}")
        temp_output.unlink(missing_ok=True)
        return False
    verified_duration = media_duration(temp_output)
    if verified_duration is None or verified_duration < 1:
        log("muxed TS verification failed; raw sources were preserved")
        temp_output.unlink(missing_ok=True)
        return False
    duration_error = abs(verified_duration - safe_duration)
    duration_tolerance = max(2.0, min(6.0, safe_duration * 0.0005))
    if duration_error > duration_tolerance:
        log(
            f"mux duration mismatch ({verified_duration:.2f}s vs {safe_duration:.2f}s, "
            f"delta={duration_error:.2f}s); raw sources were preserved"
        )
        temp_output.unlink(missing_ok=True)
        return False
    if not packet_scan_ok(temp_output):
        temp_output.unlink(missing_ok=True)
        return False
    output_path.unlink(missing_ok=True)
    temp_output.replace(output_path)
    log(
        f"part complete: {output_path.name} / {verified_duration:.1f}s / "
        f"{output_path.stat().st_size / (1024**2):.1f}MiB / {float(fps):.6f}fps / sync-verified"
    )
    if delete_sources:
        video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
    return True


def recover_orphan_sources(folder: Path, include_large: bool = False,
                           only_video: Path | None = None,
                           preserve_sources: bool = False) -> int:
    recovered = 0
    for video_path in folder.glob("*.ts.soop.h264"):
        if only_video is not None and video_path.resolve() != only_video.resolve():
            continue
        if not include_large and video_path.stat().st_size > 4 * 1024 ** 3:
            log(f"large orphan deferred to maintenance recovery: {video_path.name}")
            continue
        mutex_handle, acquired = acquire_recovery_mutex(video_path)
        if not acquired:
            log(f"orphan recovery already running; skip duplicate: {video_path.name}")
            continue
        prefix = str(video_path)[: -len(".soop.h264")]
        output_path = Path(prefix)
        audio_path = Path(prefix + ".soop.aac")
        state_path = Path(prefix + ".soop.json")
        try:
            if output_path.exists() or not audio_path.exists():
                continue
            fps = 60
            elapsed = 0.0
            video_frames = 0
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                fps = int(state.get("fps") or 60)
                elapsed = float(state.get("elapsed_sec") or 0)
                video_frames = int(state.get("video_frames") or 0)
            except Exception:
                pass
            audio_seconds = adts_duration(audio_path) or media_duration(audio_path)
            if audio_seconds and audio_seconds >= 1:
                elapsed = audio_seconds
                fps = choose_fps(video_frames, audio_seconds)
            elif elapsed < 1:
                elapsed = max(1.0, audio_path.stat().st_size * 8 / 192000)
            log(f"recovering interrupted SOOP part: {output_path.name}")
            if mux_elementary(
                video_path,
                audio_path,
                output_path,
                fps,
                elapsed,
                delete_sources=not preserve_sources,
            ):
                if not preserve_sources:
                    state_path.unlink(missing_ok=True)
                recovered += 1
        finally:
            release_recovery_mutex(mutex_handle)
    return recovered


def wait_for_high_quality(cdp: CDP, url: str, timeout: int = 75) -> dict:
    cdp.call("Page.enable")
    cdp.call("Network.enable")
    cdp.call("Page.navigate", {"url": url})
    deadline = time.monotonic() + timeout
    last_original_request = 0.0
    best_state: dict = {}
    while time.monotonic() < deadline and not STOP_REQUESTED:
        expression = """(() => {
          const player = window.livePlayer;
          const video = [...document.querySelectorAll('video')]
            .sort((a,b) => (b.videoWidth*b.videoHeight)-(a.videoWidth*a.videoHeight))[0];
          if (video) { video.muted = true; video.play().catch(() => {}); }
          const info = player?.streamConnector?.streamer?.config?.broadInfo;
          const presets = info?.viewPreset || info?.apiResponse?.CHANNEL?.VIEWPRESET || [];
          const maxPresetHeight = Math.max(0, ...presets.map(item => Number(item?.label_resolution || 0)));
          return {
            href: location.href,
            title: info?.szBroadTitleDisplay || info?.szBroadTitle ||
              info?.apiResponse?.CHANNEL?.TITLE || '',
            isOnAir: Boolean(info?.bOnAir ?? window.liveView?.LiveViewInfo?.bOnAir),
            usingAgent: Boolean(player?.streamConnector?.streamer?.isUsingAgent),
            quality: info?.quality || player?.streamConnector?.streamer?.config?.qualityInfo?.quality || '',
            maxPresetHeight,
            width: video?.videoWidth || 0,
            height: video?.videoHeight || 0,
            readyState: video?.readyState || 0,
            hasPlayer: Boolean(player),
          };
        })()"""
        try:
            state = cdp.evaluate(expression)
        except (TimeoutError, websocket.WebSocketTimeoutException):
            time.sleep(1)
            continue
        if isinstance(state, dict):
            best_state = state
            now = time.monotonic()
            expected_height = max(720, int(state.get("maxPresetHeight") or 0))
            quality = str(state.get("quality") or "").upper()
            height = int(state.get("height") or 0)
            if state.get("hasPlayer") and (quality != "ORIGINAL" or height < expected_height) and now - last_original_request >= 4:
                cdp.evaluate(
                    """(() => {
                      try { window.livePlayer?.changeQuality?.('ORIGINAL', true); return true; }
                      catch (_) { return false; }
                    })()"""
                )
                last_original_request = now
            if state.get("usingAgent") and quality == "ORIGINAL" and height >= expected_height:
                return state
        time.sleep(1)
    raise RuntimeError(f"SOOP high-quality agent was not ready: {best_state}")


def probe_live(url: str, timeout: int = 25) -> dict:
    """Determine SOOP live state, preferring the stable player API.

    The signed-in browser probe remains as a fallback for password/adult/login
    restricted rooms and for transient API failures.
    """
    requested_channel = channel_id_from_url(url).casefold()
    api_probe = probe_live_api(requested_channel)
    if api_probe and api_probe.get("is_live"):
        return api_probe

    ensure_soop_agent()
    ensure_browser()
    if requested_channel:
        existing_live = None
        try:
            targets = requests.get(f"{CDP_HTTP}/json/list", timeout=3).json()
            for existing in targets:
                if existing.get("type") != "page" or not existing.get("webSocketDebuggerUrl"):
                    continue
                existing_channel = next(
                    (
                        part.casefold()
                        for part in urlparse(str(existing.get("url") or "")).path.split("/")
                        if part
                    ),
                    "",
                )
                if existing_channel != requested_channel:
                    continue
                existing_cdp = CDP(existing["webSocketDebuggerUrl"])
                try:
                    state = existing_cdp.evaluate("""(() => {
                      const player = window.livePlayer;
                      const info = player?.streamConnector?.streamer?.config?.broadInfo;
                      const rawOnAir = info?.bOnAir ?? window.liveView?.LiveViewInfo?.bOnAir;
                      const pathParts = location.pathname.split('/').filter(Boolean);
                      const broadcastId = pathParts.length >= 2 && /^\\d+$/.test(pathParts[pathParts.length - 1])
                        ? pathParts[pathParts.length - 1] : '';
                      const videos = [...document.querySelectorAll('video')];
                      const mediaTime = videos.reduce((value, video) => Math.max(value, Number(video.currentTime) || 0), 0);
                      return {
                        title: info?.szBroadTitleDisplay || info?.szBroadTitle ||
                          info?.apiResponse?.CHANNEL?.TITLE || '',
                        onAirKnown: rawOnAir !== undefined && rawOnAir !== null,
                        isOnAir: Boolean(rawOnAir),
                        broadcastId,
                        hasPlayer: Boolean(player),
                        mediaTime,
                        mediaReady: videos.some(video => video.readyState >= 2 && !video.ended),
                      };
                    })()""")
                    # A numeric broadcast route and a player object survive after
                    # the broadcast ends.  Only trust the old capture tab when
                    # SOOP still says on-air or its media clock is advancing.
                    if (
                        isinstance(state, dict)
                        and state.get("hasPlayer")
                        and state.get("broadcastId")
                        and not (state.get("onAirKnown") and state.get("isOnAir"))
                    ):
                        before = float(state.get("mediaTime") or 0)
                        time.sleep(0.8)
                        follow = existing_cdp.evaluate("""(() => {
                          const player = window.livePlayer;
                          const videos = [...document.querySelectorAll('video')];
                          const pathParts = location.pathname.split('/').filter(Boolean);
                          const broadcastId = pathParts.length >= 2 && /^\\d+$/.test(pathParts[pathParts.length - 1])
                            ? pathParts[pathParts.length - 1] : '';
                          return {
                            hasPlayer: Boolean(player),
                            broadcastId,
                            mediaTime: videos.reduce((value, video) => Math.max(value, Number(video.currentTime) || 0), 0),
                            mediaReady: videos.some(video => video.readyState >= 2 && !video.ended),
                          };
                        })()""")
                        if not (
                            isinstance(follow, dict)
                            and follow.get("hasPlayer")
                            and follow.get("broadcastId") == state.get("broadcastId")
                            and follow.get("mediaReady")
                            and float(follow.get("mediaTime") or 0) > before + 0.2
                        ):
                            state = {}
                finally:
                    existing_cdp.close()
                if isinstance(state, dict) and state.get("hasPlayer") and state.get("broadcastId"):
                    existing_live = {
                        "ok": True,
                        "check_ok": True,
                        "is_live": True,
                        "title": str(state.get("title") or ""),
                        "broadcast_id": str(state.get("broadcastId") or ""),
                        "live_reason": "existing_capture_target",
                    }
                    if existing_live["title"]:
                        return existing_live
        except Exception:
            # If the active target cannot be inspected, use the ordinary
            # temporary player probe below.  A probe error must not terminate
            # an otherwise healthy recording.
            pass
        if existing_live:
            return existing_live
    target = create_target()
    target_id = str(target["id"])
    cdp = CDP(target["webSocketDebuggerUrl"])
    best_state: dict = {}
    try:
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": url})
        minimize_target_window(cdp, target_id)
        deadline = time.monotonic() + max(5, int(timeout))
        expression = """(() => {
          const player = window.livePlayer;
          const info = player?.streamConnector?.streamer?.config?.broadInfo;
          const rawOnAir = info?.bOnAir ?? window.liveView?.LiveViewInfo?.bOnAir;
          const pathParts = location.pathname.split('/').filter(Boolean);
          const broadcastId = pathParts.length >= 2 && /^\\d+$/.test(pathParts[pathParts.length - 1])
            ? pathParts[pathParts.length - 1] : '';
          const videos = [...document.querySelectorAll('video')];
          return {
            href: location.href,
            title: info?.szBroadTitleDisplay || info?.szBroadTitle ||
              info?.apiResponse?.CHANNEL?.TITLE || '',
            onAirKnown: rawOnAir !== undefined && rawOnAir !== null,
            isOnAir: Boolean(rawOnAir),
            broadcastId,
            hasPlayer: Boolean(player),
            mediaTime: videos.reduce((value, video) => Math.max(value, Number(video.currentTime) || 0), 0),
            mediaReady: videos.some(video => video.readyState >= 2 && !video.ended),
            readyState: document.readyState,
            playerMessage: (document.body?.innerText || '').slice(0, 600),
          };
        })()"""
        live_without_title: dict = {}
        route_media_times: dict[str, float] = {}
        while time.monotonic() < deadline and not STOP_REQUESTED:
            try:
                state = cdp.evaluate(expression)
            except (TimeoutError, websocket.WebSocketTimeoutException):
                time.sleep(0.5)
                continue
            if isinstance(state, dict):
                best_state = state
                # The current SOOP player redirects an offline channel to
                # /<channel>/null.  This is a definite platform response, not
                # a transient probe failure.  Treat it as OFF so the recorder
                # can close the session instead of creating endless empty
                # retry parts.
                href = str(state.get("href") or "").rstrip("/").lower()
                if (
                    state.get("hasPlayer")
                    and state.get("readyState") == "complete"
                    and href.endswith("/null")
                ):
                    return {
                        "ok": True,
                        "check_ok": True,
                        "is_live": False,
                        "title": str(state.get("title") or ""),
                        "offline_reason": "soop_null_broadcast",
                    }
                # A route/player object alone is not proof of LIVE: SOOP keeps
                # both around after the broadcast ends.  For a password-room
                # transition, retain LIVE only when the already-open player's
                # media clock is demonstrably advancing.
                if state.get("hasPlayer") and state.get("broadcastId"):
                    broadcast_id = str(state.get("broadcastId") or "")
                    media_time = float(state.get("mediaTime") or 0)
                    previous_media_time = route_media_times.get(broadcast_id)
                    route_media_times[broadcast_id] = media_time
                    route_is_playing = bool(
                        state.get("mediaReady")
                        and previous_media_time is not None
                        and media_time > previous_media_time + 0.2
                    )
                    if (state.get("onAirKnown") and state.get("isOnAir")) or route_is_playing:
                        live_without_title = {
                            "ok": True,
                            "check_ok": True,
                            "is_live": True,
                            "title": str(state.get("title") or ""),
                            "broadcast_id": broadcast_id,
                            "live_reason": "on_air" if state.get("isOnAir") else "active_media_clock",
                        }
                        if live_without_title["title"] or route_is_playing:
                            return live_without_title
                        time.sleep(0.5)
                        continue
                if state.get("onAirKnown"):
                    if state.get("isOnAir") and not state.get("title"):
                        live_without_title = {
                            "ok": True,
                            "check_ok": True,
                            "is_live": True,
                            "title": "",
                            "live_reason": "on_air_without_title",
                        }
                        time.sleep(0.5)
                        continue
                    return {
                        "ok": True,
                        "check_ok": True,
                        "is_live": bool(state.get("isOnAir")),
                        "title": str(state.get("title") or ""),
                    }
            time.sleep(0.5)
        if live_without_title:
            return live_without_title
        if api_probe and api_probe.get("check_ok"):
            return api_probe
        return {
            "ok": False,
            "check_ok": False,
            "is_live": False,
            "title": str(best_state.get("title") or ""),
            "error": f"SOOP player did not expose a definite on-air state: {best_state}",
        }
    finally:
        release_target(cdp, target_id)
        cdp.close()


def write_state(
    state_path: Path,
    url: str,
    width: int,
    height: int,
    video_frames: int,
    audio_frames: int,
    elapsed: float,
) -> None:
    fps = choose_fps(video_frames, elapsed)
    temp = Path(str(state_path) + ".tmp")
    temp.write_text(
        json.dumps(
            {
                "url": url,
                "width": width,
                "height": height,
                "video_frames": video_frames,
                "audio_frames": audio_frames,
                "elapsed_sec": round(elapsed, 3),
                "fps": fps,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temp.replace(state_path)


def capture(url: str, output_path: Path, idle_timeout: int, max_seconds: int = 0) -> int:
    ensure_soop_agent()
    ensure_browser()
    recover_orphan_sources(output_path.parent)
    target = create_target()
    target_id = str(target["id"])
    cdp = CDP(target["webSocketDebuggerUrl"])
    video_path = Path(str(output_path) + ".soop.h264")
    audio_path = Path(str(output_path) + ".soop.aac")
    state_path = Path(str(output_path) + ".soop.json")
    video_path.unlink(missing_ok=True)
    audio_path.unlink(missing_ok=True)
    state_path.unlink(missing_ok=True)
    video_frames = audio_frames = skipped_frames = 0
    video_bytes = audio_bytes = 0
    capture_started: float | None = None
    first_packet_at: float | None = None
    last_media_at = time.monotonic()
    state_last_written = 0.0
    reconnect_attempts = 0
    max_reconnect_attempts = 3
    width = height = 0
    try:
        ready = wait_for_high_quality(cdp, url)
        width = int(ready.get("width") or 0)
        height = int(ready.get("height") or 0)
        log(
            f"official agent connected: {width}x{height}, "
            f"quality={ready.get('quality') or 'ORIGINAL'}"
        )
        # Start the media-idle clock after the player reports ready.  Previously
        # the timeout only ran after the first packet, so a stale/offline player
        # that never produced even one frame could remain here forever.
        last_media_at = time.monotonic()
        minimize_target_window(cdp, target_id)
        with video_path.open("wb", buffering=0) as video, audio_path.open("wb", buffering=0) as audio:
            while not STOP_REQUESTED:
                if capture_started is not None and max_seconds > 0:
                    if time.monotonic() - capture_started >= max_seconds:
                        log(f"test duration reached: {max_seconds}s")
                        break
                try:
                    message = json.loads(cdp.ws.recv())
                except (TimeoutError, websocket.WebSocketTimeoutException):
                    now = time.monotonic()
                    if now - last_media_at >= idle_timeout:
                        if reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            log(
                                f"no media frames for {idle_timeout}s; trying in-place SOOP "
                                f"player recovery {reconnect_attempts}/{max_reconnect_attempts}"
                            )
                            try:
                                # First keep the already-authorized page alive.
                                # This is the important path when the streamer
                                # toggles a password on an already-open room.
                                cdp.evaluate(
                                    """(() => {
                                      try {
                                        const p = window.livePlayer;
                                        p?.changeQuality?.('ORIGINAL', true);
                                        const v = [...document.querySelectorAll('video')]
                                          .sort((a,b) => (b.videoWidth*b.videoHeight)-(a.videoWidth*a.videoHeight))[0];
                                        if (v) { v.muted = true; v.play().catch(() => {}); }
                                        return Boolean(p || v);
                                      } catch (_) { return false; }
                                    })()"""
                                )
                            except Exception as exc:
                                log(f"in-place SOOP player recovery failed: {exc}")
                            last_media_at = time.monotonic()
                            continue
                        log(
                            f"no media frames after {max_reconnect_attempts} SOOP recovery attempts; "
                            "ending part"
                        )
                        break
                    continue
                except Exception as exc:
                    if reconnect_attempts < max_reconnect_attempts:
                        reconnect_attempts += 1
                        log(
                            f"SOOP CDP stream ended ({exc}); recovery "
                            f"{reconnect_attempts}/{max_reconnect_attempts}"
                        )
                        try:
                            cdp.close()
                            cdp = CDP(target["webSocketDebuggerUrl"])
                            ready = wait_for_high_quality(cdp, url, timeout=25)
                            width = int(ready.get("width") or width)
                            height = int(ready.get("height") or height)
                        except Exception as recover_exc:
                            log(f"SOOP CDP reconnect failed: {recover_exc}")
                        last_media_at = time.monotonic()
                        continue
                    log(f"CDP stream ended after recovery attempts: {exc}")
                    break
                if message.get("method") != "Network.webSocketFrameReceived":
                    continue
                response = message.get("params", {}).get("response", {})
                if response.get("opcode") != 2:
                    continue
                try:
                    payload = base64.b64decode(response.get("payloadData", ""))
                except Exception:
                    skipped_frames += 1
                    continue
                media_kind, media, envelope_size = unpack_agent_media(payload)
                if media_kind is None:
                    skipped_frames += 1
                    if skipped_frames <= 5:
                        log(
                            "unrecognized SOOP agent binary packet: "
                            f"len={len(payload)}, prefix={payload[:24].hex(' ')}"
                        )
                    continue
                now = time.monotonic()
                if media_kind == "video":
                    types = nal_types(media)
                    if capture_started is None:
                        if 7 not in types:
                            continue
                        capture_started = now
                        log(
                            "SPS/PPS keyframe found; writing lossless H.264/AAC "
                            f"(agent envelope={envelope_size} bytes)"
                        )
                    video.write(media)
                    video_frames += 1
                    video_bytes += len(media)
                elif media_kind == "audio":
                    if capture_started is None:
                        continue
                    audio.write(media)
                    audio_frames += 1
                    audio_bytes += len(media)
                if first_packet_at is None:
                    first_packet_at = now
                last_media_at = now
                reconnect_attempts = 0
                if capture_started and now - state_last_written >= 5:
                    elapsed = max(0.001, now - capture_started)
                    write_state(
                        state_path,
                        url,
                        width,
                        height,
                        video_frames,
                        audio_frames,
                        elapsed,
                    )
                    state_last_written = now
        if capture_started is None:
            log("no decodable keyframe was received")
            return 4
        elapsed = max(0.001, last_media_at - capture_started)
        audio_seconds = adts_duration(audio_path) or media_duration(audio_path)
        timing_duration = audio_seconds if audio_seconds and audio_seconds >= 1 else elapsed
        fps = choose_fps(video_frames, timing_duration)
        write_state(state_path, url, width, height, video_frames, audio_frames, elapsed)
        log(
            f"captured {timing_duration:.1f}s media: video={video_frames} frames/{video_bytes/1024**2:.1f}MiB, "
            f"audio={audio_frames} packets/{audio_bytes/1024**2:.1f}MiB, skipped={skipped_frames}"
        )
        if not mux_elementary(video_path, audio_path, output_path, fps, timing_duration):
            return 5
        state_path.unlink(missing_ok=True)
        return 0
    finally:
        release_target(cdp, target_id)
        cdp.close()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Record lossless SOOP agent frames through a dedicated browser")
    parser.add_argument("url")
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument("--probe", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--idle-timeout", type=int, default=35)
    parser.add_argument("--max-seconds", type=int, default=0, help=argparse.SUPPRESS)
    args = parser.parse_args()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, request_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, request_stop)
    try:
        if args.probe:
            result = probe_live(args.url)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            return 0 if result.get("check_ok") else 4
        if args.output is None:
            parser.error("output is required unless --probe is used")
        return capture(
            args.url,
            args.output.resolve(),
            max(15, args.idle_timeout),
            max(0, args.max_seconds),
        )
    except KeyboardInterrupt:
        request_stop()
        return 130
    except Exception as exc:
        log(f"fatal: {type(exc).__name__}: {exc}")
        return 6


if __name__ == "__main__":
    raise SystemExit(main())
