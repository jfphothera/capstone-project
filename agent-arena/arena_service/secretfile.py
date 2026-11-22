"""Producer credential files, protected with Windows DPAPI (CurrentUser) when available.

A `.dpapi` file can only be decrypted by the Windows account that wrote it. On
other platforms (tests) a plain JSON file is accepted and must be ACL-protected.
"""
from __future__ import annotations

import ctypes
import json
import os
import sys


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    target = _Blob()
    entropy_raw = b"agent-arena-producer"
    entropy_buf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = _Blob(len(entropy_raw), ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_char)))
    call = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = call(ctypes.byref(source), "agent-arena", ctypes.byref(entropy), None, None, 0x1, ctypes.byref(target))
    else:
        ok = call(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(target))
    if not ok:
        raise OSError("DPAPI operation failed: " + str(ctypes.GetLastError()))
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def write(path, credential_id, secret):
    raw = json.dumps({"credential_id": credential_id, "secret": secret}).encode()
    if path.endswith(".dpapi"):
        raw = _dpapi(raw, True)
    with open(path, "wb") as handle:
        handle.write(raw)


def read(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    if path.endswith(".dpapi"):
        if sys.platform != "win32":
            raise OSError("DPAPI credential files need Windows")
        raw = _dpapi(raw, False)
    data = json.loads(raw)
    return data["credential_id"], data["secret"]


def main(argv=None):
    """python -m arena_service.secretfile <out.dpapi>  (reads the JSON printed by producer-issue on stdin)"""
    argv = argv or sys.argv[1:]
    data = json.loads(sys.stdin.read())
    write(argv[0], data["credential_id"], data["secret"])
    print(f"wrote {os.path.abspath(argv[0])} (credential {data['credential_id']})")


if __name__ == "__main__":
    main()
