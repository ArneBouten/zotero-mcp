"""Settings of Arne's Zotero extension for Claude Desktop.

Since 07-11-2026 Claude Desktop starts zotero-mcp through the "Zotero" extension
(local.mcpb.arne-bouten.zotero) instead of a ``claude_desktop_config.json``
entry. Its environment comes from the extension's manifest (``server.mcp_config.env``,
with ``${user_config.<key>}`` placeholders) and its settings file
(``Claude Extensions Settings/<id>.json``), where Claude Desktop stores the API
keys encrypted (Electron safeStorage: ``__encrypted__:`` + base64 of ``v10``,
a 12-byte nonce and AES-256-GCM ciphertext; the AES key sits DPAPI-protected in
the app's ``Local State``).

CLI commands run outside Claude Desktop (update-db, setup-info, the Zotero
actions) read the same values here, so the keys live in one place. Decryption
works on Windows for the signed-in user only; elsewhere encrypted values are
skipped.
"""

from __future__ import annotations

import base64
import glob
import json
import os
import re
import sys
from pathlib import Path

ENCRYPTED_PREFIX = "__encrypted__:"
_PLACEHOLDER = re.compile(r"\$\{user_config\.([A-Za-z0-9_]+)\}")


def claude_data_roots() -> list[Path]:
    """Claude Desktop data folders that can hold extensions, most likely first."""
    roots: list[Path] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        roaming = os.environ.get("APPDATA")
        if local:
            roots += [Path(p) for p in sorted(glob.glob(os.path.join(local, "Claude-Data", "claude_*")))]
            roots += [Path(p) for p in sorted(glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")))]
        if roaming:
            roots.append(Path(roaming) / "Claude")
    elif sys.platform == "darwin":
        roots.append(Path.home() / "Library" / "Application Support" / "Claude")
    else:
        roots.append(Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "Claude")
    return [r for r in roots if r.is_dir()]


def find_zotero_extension(roots: list[Path] | None = None):
    """Return (root, manifest, settings) of the installed Zotero extension, or None."""
    for root in roots if roots is not None else claude_data_roots():
        ext_dir = root / "Claude Extensions"
        if not ext_dir.is_dir():
            continue
        for manifest_path in sorted(ext_dir.glob("*/manifest.json")):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if manifest.get("name") != "zotero":
                continue
            settings_path = root / "Claude Extensions Settings" / f"{manifest_path.parent.name}.json"
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
            except Exception:
                settings = {}
            return root, manifest, settings
    return None


def extension_installed(roots: list[Path] | None = None) -> bool:
    return find_zotero_extension(roots) is not None


def _safe_storage_key(root: Path) -> bytes:
    """The AES key Claude Desktop uses for encrypted settings (Windows, current user)."""
    import ctypes
    import ctypes.wintypes as w

    state = json.loads((root / "Local State").read_text(encoding="utf-8"))
    blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if blob[:5] != b"DPAPI":
        raise ValueError("unexpected Local State key format")
    data = blob[5:]

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", w.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI refused to unlock Claude Desktop's key")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def decrypt_value(value: str, key: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = base64.b64decode(value[len(ENCRYPTED_PREFIX):])
    if raw[:3] != b"v10":
        raise ValueError("unknown encryption version")
    return AESGCM(key).decrypt(raw[3:15], raw[15:], None).decode("utf-8")


def load_extension_env(roots: list[Path] | None = None, key_loader=_safe_storage_key) -> dict:
    """Environment the Zotero extension gives the server, with secrets decrypted.

    Empty when the extension is not installed or is switched off. Values that
    can't be decrypted are left out rather than passed on encrypted.
    """
    found = find_zotero_extension(roots)
    if not found:
        return {}
    root, manifest, settings = found
    if settings.get("isEnabled") is False:
        return {}
    user_config = settings.get("userConfig") or {}
    fields = manifest.get("user_config") or {}
    key: bytes | None = None
    key_failed = False

    def resolve(name: str) -> str | None:
        nonlocal key, key_failed
        value = user_config.get(name, (fields.get(name) or {}).get("default"))
        if value is None:
            return None
        value = str(value)
        if value.startswith(ENCRYPTED_PREFIX):
            if key_failed:
                return None
            try:
                if key is None:
                    key = key_loader(root)
                return decrypt_value(value, key)
            except Exception:
                key_failed = True
                return None
        return value

    env = {}
    template = ((manifest.get("server") or {}).get("mcp_config") or {}).get("env") or {}
    for name, raw in template.items():
        missing = False

        def sub(m):
            nonlocal missing
            v = resolve(m.group(1))
            if v is None:
                missing = True
                return ""
            return v

        value = _PLACEHOLDER.sub(sub, str(raw))
        if not missing and value != "":
            env[name] = value
    return env