"""Windows-friendly JSON reader that does not block atomic file replacement."""

import ctypes
import json
import os
from pathlib import Path


def read_json_shared(path):
    path = Path(path)
    if os.name != "nt":
        return json.loads(path.read_text(encoding="utf-8-sig"))

    kernel32 = ctypes.windll.kernel32
    create_file = kernel32.CreateFileW
    create_file.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        str(path),
        0x80000000,              # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ|WRITE|DELETE
        None,
        3,                       # OPEN_EXISTING
        0x00000080,              # FILE_ATTRIBUTE_NORMAL
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if not handle or handle == invalid:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed: {path}")
    try:
        size = ctypes.c_longlong()
        if not kernel32.GetFileSizeEx(handle, ctypes.byref(size)):
            raise OSError(ctypes.get_last_error(), f"GetFileSizeEx failed: {path}")
        if size.value <= 0:
            raise ValueError(f"empty JSON file: {path}")
        if size.value > 16 * 1024 * 1024:
            raise ValueError(f"JSON file too large: {path}")
        buf = ctypes.create_string_buffer(size.value)
        read = ctypes.c_ulong()
        if not kernel32.ReadFile(handle, buf, size.value, ctypes.byref(read), None):
            raise OSError(ctypes.get_last_error(), f"ReadFile failed: {path}")
        return json.loads(buf.raw[:read.value].decode("utf-8-sig"))
    finally:
        kernel32.CloseHandle(handle)
