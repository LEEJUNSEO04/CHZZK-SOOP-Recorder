
import json
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
APP_NAME = "CHZZK Stable TS Sweeper v1.1 Title-Safe Compatible"


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, icon="ℹ️"):
    print(f"[{now()}] {icon} {msg}", flush=True)


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))


def load_state(path):
    if not path.exists():
        return {"files": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"files": {}}


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def sanitize(text):
    text = str(text or "").strip()
    for ch in '<>:"/\\|?*\n\r\t':
        text = text.replace(ch, "_")
    text = re.sub(r"\s+", "_", text).strip(" ._")
    return text or "unknown"


def read_streamer_names():
    path = BASE_DIR / "streamers.txt"
    if not path.exists():
        return []

    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) >= 4 and parts[0] in ["0", "1"]:
            names.append(parts[1])
        elif len(parts) >= 1:
            names.append(parts[0])

    return sorted(set(names), key=len, reverse=True)


def infer_base_and_part(ts_path):
    stem = ts_path.stem
    m = re.match(r"(.+)_part(\d+)$", stem)
    if m:
        return m.group(1), int(m.group(2))
    return stem, None


def infer_streamer(base_name, streamer_names):
    """
    새 세션명은 방송 제목을 빼고 2026-06-02_스트리머_22-37-52 형태를 사용합니다.
    구버전 2026-06-02_방송제목_스트리머_22-37-52 형태도 계속 처리합니다.
    """
    for name in streamer_names:
        if f"_{name}_" in base_name or base_name.endswith(f"_{name}"):
            return name

    # 등록 직후처럼 목록에 아직 없더라도 복합 이름의 마지막 단어만
    # 떼어내지 않습니다. 새 형식의 날짜/시각 사이 전체를 보존합니다.
    m = re.match(r"^\d{4}-\d{2}-\d{2}_(.+)_\d{2}-\d{2}-\d{2}$", base_name)
    if m:
        return m.group(1).replace("_", " ").strip()

    return "unknown"


def ffmpeg_copy(ts_path, mp4_path):
    log(f"변환 시작: {ts_path.name} → {mp4_path.name}", "🎬")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", str(ts_path), "-c", "copy", str(mp4_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )

    if proc.returncode != 0:
        log(f"변환 실패: {ts_path.name}", "❌")
        print(proc.stderr[-2000:], flush=True)
        return False

    if not mp4_path.exists() or mp4_path.stat().st_size <= 0:
        log("변환 실패: mp4 없음/0바이트", "❌")
        return False

    log(f"변환 완료: {mp4_path}", "✅")
    return True


def safe_delete(path, attempts=12, delay=2):
    p = Path(path)

    for i in range(attempts):
        try:
            p.unlink(missing_ok=True)
            log(f"로컬 ts 삭제 완료: {p.name}", "🧹")
            return True
        except PermissionError:
            log(f"파일 잠김. 삭제 재시도 {i+1}/{attempts}: {p.name}", "⚠️")
            time.sleep(delay)
        except FileNotFoundError:
            return True
        except Exception as e:
            log(f"삭제 실패: {p} / {e}", "❌")
            return False

    log(f"삭제 보류. 다음 루프에서 다시 확인: {p.name}", "⚠️")
    return False


def process_ts(ts_path, cfg, streamer_names):
    paths = cfg["paths"]
    merged_dir = Path(paths["merged_dir"])
    drive_root = Path(paths["drive_dir"])

    base_name, part_no = infer_base_and_part(ts_path)
    streamer = infer_streamer(base_name, streamer_names)
    month = datetime.now().strftime("%Y-%m")

    if part_no is not None:
        parts_dir = drive_root / sanitize(streamer) / month / f"{base_name}_parts"
    else:
        parts_dir = drive_root / sanitize(streamer) / month / f"{base_name}_orphan_parts"

    parts_dir.mkdir(parents=True, exist_ok=True)

    local_mp4 = merged_dir / f"{ts_path.stem}.mp4"
    drive_mp4 = parts_dir / f"{ts_path.stem}.mp4"

    if drive_mp4.exists():
        log(f"Drive에 이미 mp4 있음. ts 삭제만 시도: {drive_mp4.name}", "🧩")
        safe_delete(ts_path)
        return True

    if not ffmpeg_copy(ts_path, local_mp4):
        return False

    try:
        shutil.move(str(local_mp4), str(drive_mp4))
        log(f"Google Drive parts 이동 완료: {drive_mp4}", "☁️")
    except Exception as e:
        log(f"Drive 이동 실패: {e}", "❌")
        return False

    safe_delete(ts_path)
    return True


def main():
    cfg = load_config()
    paths = cfg["paths"]

    temp_dir = Path(paths["temp_dir"])
    logs_dir = Path(paths["logs_dir"])
    logs_dir.mkdir(parents=True, exist_ok=True)

    state_path = logs_dir / "stable_ts_sweeper_state.json"
    state = load_state(state_path)

    streamer_names = read_streamer_names()

    sw = cfg.setdefault("sweeper", {})
    interval_sec = int(sw.get("scan_interval_sec", 30))
    stable_sec = int(sw.get("stable_seconds", 300))
    min_age_sec = int(sw.get("min_age_seconds", 120))
    min_size_mb = float(sw.get("min_size_mb", 5))
    enabled = bool(sw.get("enabled", True))

    print("=" * 72)
    log(APP_NAME, "🚀")
    log(f"감시 폴더: {temp_dir}", "📁")
    log(f"스캔 주기: {interval_sec}초", "⏱️")
    log(f"안정 판단: {stable_sec}초 동안 크기 변화 없음", "🧊")
    log(f"최소 파일 나이: {min_age_sec}초", "🕒")
    print("=" * 72)

    if not enabled:
        log("sweeper.enabled=false 상태입니다.", "⛔")
        return

    while True:
        try:
            # 실행 중 streamers.txt에 추가된 스트리머도 즉시 반영합니다.
            streamer_names = read_streamer_names()
            current = set()

            for ts in sorted(temp_dir.glob("*.ts"), key=lambda p: p.stat().st_mtime if p.exists() else 0):
                key = str(ts.resolve())
                current.add(key)

                try:
                    stat = ts.stat()
                    size = stat.st_size
                    age = time.time() - stat.st_mtime
                except Exception:
                    continue

                if size <= min_size_mb * 1024 * 1024:
                    continue

                info = state["files"].get(key)
                if not info:
                    state["files"][key] = {
                        "size": size,
                        "first_seen": time.time(),
                        "last_change": time.time(),
                        "last_seen": time.time(),
                    }
                    continue

                if int(info.get("size", 0)) != int(size):
                    info["size"] = size
                    info["last_change"] = time.time()
                    info["last_seen"] = time.time()
                    continue

                info["last_seen"] = time.time()
                stable_for = time.time() - float(info.get("last_change", time.time()))

                if age < min_age_sec:
                    continue

                if stable_for >= stable_sec:
                    log(f"안정된 ts 발견: {ts.name} / {round(size/1024/1024,2)}MB / 안정 {int(stable_for)}초", "🧊")
                    if process_ts(ts, cfg, streamer_names):
                        state["files"].pop(key, None)
                        save_state(state_path, state)

            for key in list(state["files"].keys()):
                if key not in current and not Path(key).exists():
                    state["files"].pop(key, None)

            save_state(state_path, state)
            time.sleep(interval_sec)

        except KeyboardInterrupt:
            log("Stable TS Sweeper 종료", "🛑")
            break
        except Exception as e:
            log(f"Sweeper 오류: {e}", "❌")
            time.sleep(interval_sec)


if __name__ == "__main__":
    main()
