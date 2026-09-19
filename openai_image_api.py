"""Small, dependency-light OpenAI Image API client for Recorder thumbnails.

The API key is encrypted with Windows DPAPI so it never needs to be stored in
config.json or a plain-text .env file.  The runtime HTTP dependency is the
same requests package used elsewhere by the recorder.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_KEY_PATH = BASE_DIR / "logs" / "openai_image_api_key.dpapi"
API_ROOT = "https://api.openai.com/v1"


class OpenAIImageError(RuntimeError):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OpenAIImageError("DPAPI key storage requires Windows")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def key_path_from_config(config: dict | None) -> Path:
    raw = (((config or {}).get("youtube") or {}).get("thumbnail") or {})
    value = str(raw.get("openai_key_file") or "").strip()
    return Path(value) if value else DEFAULT_KEY_PATH


def save_api_key(path: Path, api_key: str) -> None:
    clean = str(api_key or "").strip().strip('"').strip("'")
    if not clean.startswith("sk-") or len(clean) < 20:
        raise OpenAIImageError("OpenAI API key format is not valid")
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = base64.b64encode(_dpapi(clean.encode("utf-8"), protect=True))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(protected)
    tmp.replace(path)


def load_api_key(path: Path | None = None) -> str:
    env_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if env_key:
        return env_key
    path = path or DEFAULT_KEY_PATH
    if not path.exists():
        return ""
    try:
        protected = base64.b64decode(path.read_bytes(), validate=True)
        return _dpapi(protected, protect=False).decode("utf-8").strip()
    except Exception as exc:
        raise OpenAIImageError(f"Encrypted OpenAI API key could not be read: {exc}") from exc


def _api_error(response: requests.Response) -> str:
    try:
        payload = response.json()
        message = str((payload.get("error") or {}).get("message") or "").strip()
        code = str((payload.get("error") or {}).get("code") or "").strip()
        if message:
            return f"HTTP {response.status_code} {code}: {message}".strip()
    except Exception:
        pass
    return f"HTTP {response.status_code}: {(response.text or '')[:800]}"


def validate_api_key(api_key: str, *, timeout: int = 30) -> dict:
    response = requests.get(
        f"{API_ROOT}/models/gpt-image-2",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise OpenAIImageError(_api_error(response))
    return response.json()


def edit_image(
    api_key: str,
    reference_path: Path,
    output_path: Path,
    prompt: str,
    *,
    model: str = "gpt-image-2",
    quality: str = "medium",
    size: str = "1536x864",
    output_format: str = "jpeg",
    output_compression: int = 86,
    timeout: int = 420,
) -> Path:
    """Create one image edit and atomically persist its base64 response."""
    reference_path = Path(reference_path)
    if not reference_path.is_file():
        raise OpenAIImageError(f"Reference image is missing: {reference_path}")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    mime = "image/png" if reference_path.suffix.lower() == ".png" else "image/jpeg"
    with reference_path.open("rb") as image_file:
        response = requests.post(
            f"{API_ROOT}/images/edits",
            headers={"Authorization": f"Bearer {api_key}"},
            data={
                "model": str(model),
                "prompt": str(prompt),
                "quality": str(quality),
                "size": str(size),
                "output_format": str(output_format),
                "output_compression": str(max(1, min(100, int(output_compression)))),
            },
            files={"image[]": (reference_path.name, image_file, mime)},
            timeout=timeout,
        )
    if response.status_code >= 400:
        raise OpenAIImageError(_api_error(response))
    try:
        item = (response.json().get("data") or [])[0]
        encoded = item.get("b64_json")
        if not encoded:
            raise ValueError("b64_json is missing")
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise OpenAIImageError(f"Image API response did not contain a valid image: {exc}") from exc
    if len(image_bytes) < 20_000:
        raise OpenAIImageError("Image API returned an unexpectedly small image")
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_bytes(image_bytes)
    tmp.replace(output_path)
    return output_path


def _main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI Image API key setup for Recorder")
    parser.add_argument("--save-key", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_PATH)
    args = parser.parse_args()
    try:
        if args.save_key:
            print("OpenAI API key (input hidden): ", end="", flush=True)
            key = getpass.getpass("")
            print("Checking key and GPT-Image-2 access...", flush=True)
            model = validate_api_key(key)
            save_api_key(args.key_file, key)
            print(f"[SUCCESS] Encrypted API key saved: {args.key_file}")
            print(f"[SUCCESS] Model access confirmed: {model.get('id') or 'gpt-image-2'}")
            return 0
        if args.status:
            key = load_api_key(args.key_file)
            if not key:
                print("[NOT SET] OpenAI Image API key is not configured.")
                return 2
            model = validate_api_key(key)
            print(f"[OK] Key and model access are valid: {model.get('id') or 'gpt-image-2'}")
            return 0
        parser.print_help()
        return 1
    except Exception as exc:
        print(f"[FAILED] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
