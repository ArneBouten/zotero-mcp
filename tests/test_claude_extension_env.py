"""The Zotero extension's settings feed the CLI (Arne's build, 07-11-2026).

Claude Desktop starts zotero-mcp through the "Zotero" extension; CLI commands
read the same settings, with encrypted values decrypted. The AES key loader is
replaced so no real DPAPI or Claude Desktop data is touched.
"""

import base64
import json
import os

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zotero_mcp import claude_extension as ce

KEY = bytes(range(32))


def _encrypt(value: str) -> str:
    nonce = os.urandom(12)
    data = b"v10" + nonce + AESGCM(KEY).encrypt(nonce, value.encode(), None)
    return ce.ENCRYPTED_PREFIX + base64.b64encode(data).decode()


def _install(root, settings, enabled=True):
    ext = root / "Claude Extensions" / "local.mcpb.arne-bouten.zotero"
    ext.mkdir(parents=True)
    (ext / "manifest.json").write_text(json.dumps({
        "name": "zotero",
        "server": {"mcp_config": {"env": {
            "ZOTERO_LOCAL": "true",
            "ZOTERO_LIBRARY_ID": "${user_config.zotero_library_id}",
            "GEMINI_API_KEY": "${user_config.gemini_api_key}",
            "VOYAGE_API_KEY": "${user_config.voyage_api_key}",
        }}},
        "user_config": {
            "zotero_library_id": {"type": "string", "default": "7756991"},
            "gemini_api_key": {"type": "string", "sensitive": True},
            "voyage_api_key": {"type": "string", "sensitive": True},
        },
    }))
    sdir = root / "Claude Extensions Settings"
    sdir.mkdir(parents=True)
    (sdir / "local.mcpb.arne-bouten.zotero.json").write_text(json.dumps({"isEnabled": enabled, "userConfig": settings}))


def test_decrypts_secrets_and_fills_defaults(tmp_path):
    _install(tmp_path, {"gemini_api_key": _encrypt("gem-secret")})
    env = ce.load_extension_env([tmp_path], key_loader=lambda root: KEY)
    assert env == {"ZOTERO_LOCAL": "true", "ZOTERO_LIBRARY_ID": "7756991", "GEMINI_API_KEY": "gem-secret"}


def test_undecryptable_values_are_left_out(tmp_path):
    _install(tmp_path, {"gemini_api_key": _encrypt("gem-secret"), "voyage_api_key": "plain-voyage"})

    def no_key(root):
        raise OSError("no DPAPI here")

    env = ce.load_extension_env([tmp_path], key_loader=no_key)
    assert "GEMINI_API_KEY" not in env
    assert env["VOYAGE_API_KEY"] == "plain-voyage"


def test_switched_off_extension_gives_nothing(tmp_path):
    _install(tmp_path, {"voyage_api_key": "plain"}, enabled=False)
    assert ce.load_extension_env([tmp_path], key_loader=lambda root: KEY) == {}
    assert ce.extension_installed([tmp_path])


def test_no_extension(tmp_path):
    assert ce.load_extension_env([tmp_path]) == {}
    assert not ce.extension_installed([tmp_path])


def test_setup_does_not_add_a_config_entry_when_the_extension_is_installed(tmp_path, monkeypatch):
    from zotero_mcp import setup_helper

    monkeypatch.setattr(ce, "extension_installed", lambda roots=None: True)
    config = tmp_path / "claude_desktop_config.json"
    assert setup_helper.update_claude_config(config, "zotero-mcp") is True
    assert not config.exists()