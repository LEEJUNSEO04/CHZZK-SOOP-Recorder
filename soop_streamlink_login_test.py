from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from streamlink import Streamlink


BASE_DIR = Path(__file__).resolve().parent
FFMPEG = Path(shutil.which("ffmpeg") or (BASE_DIR / "tools" / "ffmpeg.exe"))
PROBE_FILE = BASE_DIR / ".soop_authenticated_quality_probe.ts"
DEFAULT_URL = ""


def inspect_resolution(path: Path) -> tuple[int, int, str]:
    proc = subprocess.run(
        [
            str(FFMPEG),
            "-hide_banner",
            "-i",
            str(path),
            "-t",
            "0.1",
            "-map",
            "0:v:0",
            "-f",
            "null",
            "NUL",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    match = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", output)
    if not match:
        raise RuntimeError("저장된 샘플에서 영상 해상도를 읽지 못했습니다.")
    return int(match.group(1)), int(match.group(2)), output


def record_probe(stream, path: Path, seconds: float = 6.0, max_bytes: int = 24 * 1024 * 1024) -> int:
    path.unlink(missing_ok=True)
    source = stream.open()
    started = time.monotonic()
    written = 0
    try:
        with path.open("wb") as output:
            while time.monotonic() - started < seconds and written < max_bytes:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                written += len(chunk)
    finally:
        source.close()
    return written


def main() -> int:
    print("=" * 66)
    print("SOOP 로그인 Streamlink 실제 1440p 확인")
    print("비밀번호는 화면에 표시되거나 파일에 저장되지 않습니다.")
    print("Streamlink 로그인 쿠키만 현재 Windows 사용자 영역에 저장됩니다.")
    print("기존 CHZZK Recorder/업로더는 시작·종료·리로드하지 않습니다.")
    print("=" * 66)

    url = input("SOOP 채널 주소: ").strip()
    if not url:
        print("[실패] SOOP 채널 주소가 비어 있습니다.")
        return 2
    username = input("SOOP 아이디: ").strip()
    if not username:
        print("[실패] 아이디가 비어 있습니다.")
        return 2
    password = getpass.getpass("SOOP 비밀번호(입력해도 화면에 보이지 않음): ")
    if not password:
        print("[실패] 비밀번호가 비어 있습니다.")
        return 2
    if not FFMPEG.exists():
        print(f"[실패] ffmpeg를 찾을 수 없습니다: {FFMPEG}")
        return 2

    print("\n[1/3] SOOP 로그인 및 화질 목록 확인 중...")
    try:
        session = Streamlink()
        plugin_name, plugin_class, resolved_url = session.resolve_url(url)
        if plugin_name != "soop":
            raise RuntimeError(f"SOOP 주소로 인식되지 않았습니다: {plugin_name}")

        plugin = plugin_class(session, resolved_url)
        plugin.options.set("username", username)
        plugin.options.set("password", password)
        streams = plugin.streams()
    except Exception as exc:
        print(f"[실패] 로그인/화질 조회 오류: {exc}")
        return 1
    finally:
        password = ""

    names = sorted(name for name in streams if name not in {"best", "worst"})
    print("확인된 화질:", ", ".join(names) if names else "없음")
    if "1440p" not in streams:
        print("[실패] 로그인 세션에서도 1440p 항목이 제공되지 않았습니다.")
        return 1

    print("[2/3] 1440p 항목을 별도 샘플로 약 6초간 저장 중...")
    try:
        written = record_probe(streams["1440p"], PROBE_FILE)
        if written < 188 * 20:
            raise RuntimeError(f"샘플이 너무 작습니다: {written:,} bytes")

        print(f"샘플 크기: {written / 1024 / 1024:.2f} MB")
        print("[3/3] 실제 영상 해상도 확인 중...")
        width, height, _ = inspect_resolution(PROBE_FILE)
        print(f"실제 해상도: {width}x{height}")

        if width >= 2500 and height >= 1400:
            print("\n[성공] 진짜 1440p 원본 스트림입니다.")
            print("이 로그인 쿠키를 Recorder에 안전하게 연동할 수 있습니다.")
            return 0

        print("\n[실패] 1440p라는 이름과 달리 실제 영상은 1440p가 아닙니다.")
        print("이 결과에서는 Recorder에 적용하지 않는 것이 안전합니다.")
        return 1
    except Exception as exc:
        print(f"[실패] 샘플 저장/검사 오류: {exc}")
        return 1
    finally:
        PROBE_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n사용자가 취소했습니다.")
        raise SystemExit(130)
