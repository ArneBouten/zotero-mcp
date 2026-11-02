#!/usr/bin/env python3
"""Builds this folder into a wheel and installs it for Claude Desktop.

Claude Desktop starts zotero-mcp.exe from Python 3.12's Scripts folder
(`py -3.12`), so that is where it goes, on the PC and on the laptop alike.

    uv run --no-project scripts/install_build.py      (or: py -3.12 scripts\\install_build.py)

Close Claude Desktop first: Windows can't replace zotero-mcp.exe while it runs. Or add
--while-running: the running exe is renamed aside and the new build starts with the next
Claude Desktop restart.
Never use `zotero-mcp update` or `pip install --upgrade zotero-mcp-server`:
those replace this build with the plain PyPI release.

Standard library only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path.home() / ".config" / "zotero-mcp" / "installed-build.json"

# The extras this setup uses: Gemini embeddings and Voyage re-ranking (no local
# sentence-transformers model), PDF tools, Word citations. chromadb is pinned
# to the PC's version: an index made by one chromadb version must be read by
# the same version (the laptop started from a copy of the PC's index).
EXTRAS = "pdf,scite,word"
EXTRA_PACKAGES = ["chromadb==1.5.9", "openai", "google-genai", "tiktoken"]


def target_python() -> list[str]:
    return ["py", "-3.12"] if os.name == "nt" else [sys.executable]


def run(cmd: list[str], **kw) -> None:
    print("  > " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def server_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq zotero-mcp.exe", "/NH"], capture_output=True, text=True
    ).stdout
    return "zotero-mcp.exe" in out.lower()


def move_running_exes() -> list[str]:
    """Windows can't overwrite a running .exe but can rename it: move zotero-mcp*.exe aside so
    pip can install the new ones. The running servers keep the old code until Claude Desktop
    restarts them. Old copies from earlier installs that no longer run go to _old."""
    scripts = subprocess.run(
        [*target_python(), "-c", "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'Scripts'))"],
        capture_output=True, text=True,
    ).stdout.strip()
    folder = Path(scripts)
    moved = []
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    old_dir = folder / "_old"
    for stale in folder.glob("zotero-mcp*.exe.old-*"):
        try:
            old_dir.mkdir(exist_ok=True)
            stale.rename(old_dir / stale.name)
        except OSError:
            pass  # still running from an earlier install
    for exe in folder.glob("zotero-mcp*.exe"):
        target = exe.with_name(f"{exe.name}.old-{stamp}")
        exe.rename(target)
        moved.append(target.name)
    return moved


def main() -> int:
    while_running = "--while-running" in sys.argv
    if server_running():
        if not while_running:
            print("zotero-mcp.exe is running (Claude Desktop, or an index update it started).")
            print("Quit Claude Desktop (tray icon -> Quit), wait a moment, and run this again,")
            print("or run it with --while-running: the new build is then used after Claude Desktop restarts.")
            return 1
        print("zotero-mcp.exe is running; moved aside: " + ", ".join(move_running_exes()))

    try:
        subprocess.run([*target_python(), "--version"], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        print("Python 3.12 not found (`py -3.12`). Install it first:  winget install -e --id Python.Python.3.12")
        return 1

    version = re.search(
        r'__version__\s*=\s*"([^"]+)"', (ROOT / "src" / "zotero_mcp" / "_version.py").read_text("utf-8")
    ).group(1)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "pyproject.toml"], cwd=ROOT, capture_output=True, text=True
    ).stdout.strip()
    print(f"Building zotero-mcp {version} from {ROOT}" + (" (with uncommitted changes)" if dirty else ""))

    with tempfile.TemporaryDirectory() as tmp:
        if shutil.which("uv"):
            run(["uv", "build", "--wheel", "--out-dir", tmp, str(ROOT)])
        else:
            run([*target_python(), "-m", "pip", "wheel", "--no-deps", "-w", tmp, str(ROOT)])
        wheels = sorted(Path(tmp).glob("zotero_mcp_server-*.whl"))
        if not wheels:
            print("No wheel was built.")
            return 1
        whl = str(wheels[-1])
        print("Installing it for Claude Desktop (py -3.12) ...")
        # 1. dependencies (new ones only; what is there stays), 2. this build's code, always replaced,
        #    even when the version number did not change.
        run([*target_python(), "-m", "pip", "install", "--disable-pip-version-check", f"{whl}[{EXTRAS}]", *EXTRA_PACKAGES])
        run([*target_python(), "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", "--no-deps", whl])

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(
            {
                "version": version,
                "commit": commit,
                "uncommitted_changes": bool(dirty),
                "source": str(ROOT),
                "installed_at": dt.datetime.now().isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        "utf-8",
    )
    exe = subprocess.run(
        [*target_python(), "-c", "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'Scripts','zotero-mcp.exe'))"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"\nInstalled zotero-mcp {version}.")
    print(f"Server program for Claude Desktop: {exe}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as e:
        print(f"\nStopped: {' '.join(map(str, e.cmd))} failed (exit code {e.returncode}).")
        raise SystemExit(1)
