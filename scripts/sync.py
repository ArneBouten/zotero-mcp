#!/usr/bin/env python3
"""Keeps the two computers (PC and laptop) in step through GitHub.

git does not sync by itself the way OneDrive or Google Drive do, and the
zotero-mcp that Claude Desktop runs is an installed copy, not this folder.
This script checks both:

    scripts\\sync.cmd            check now; asks before pulling, pushing or installing
    scripts\\sync.cmd --check    report only, change nothing
    scripts\\sync.cmd --no-install   git only (the set-up script installs afterwards)
    (automatic)                 when this folder is opened in VS Code (.vscode/tasks.json)

What it tells you:
 1. GitHub has commits this computer doesn't have (pushed from the other
    computer) -> "Pull now? [Y/n]".
 2. This computer has commits GitHub doesn't have (you forgot to push)
    -> "Push now? [Y/n]".
 3. Changed files that aren't committed yet: they exist only on this computer.
 4. The installed zotero-mcp (what Claude Desktop runs) is older than this
    folder -> "Build and install it now? [y/N]" (scripts/install_build.py).

It never pulls, pushes or installs without a yes, and it stops (changing
nothing) when the same file was changed on both computers.

Standard library only, so any Python 3.10+ can run it (uv or py).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TTY = sys.stdout.isatty()

# Never ask for credentials during the quiet checks
QUIET_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def _c(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if TTY else s


def say(s: str = "") -> None:
    print(s, flush=True)


def head(s: str) -> None:
    say(f"{_c('33', '!')} {s}")


def good(s: str) -> None:
    say(f"{_c('32', 'OK')} {s}")


def git(*args: str, timeout: float | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        env=QUIET_ENV,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
    ).stdout
    # rstrip only: `git status --porcelain` lines start with a meaningful space
    return out.rstrip()


def git_loud(*args: str) -> bool:
    """Runs a git command visibly (it may show progress or ask to sign in)."""
    return subprocess.run(["git", *args], cwd=ROOT).returncode == 0


def lines(out: str) -> list[str]:
    return [line for line in out.replace("\r", "").split("\n") if line]


def ask(question: str, default_yes: bool) -> bool:
    """Yes/no question; Enter takes the default, no answer within 60 s counts as no."""
    answer: list[str] = []

    def read() -> None:
        try:
            answer.append(input(f"  {question} {'[Y/n]' if default_yes else '[y/N]'} "))
        except EOFError:
            pass

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(60)
    if not answer:
        say("\n  (no answer, skipped)")
        return False
    v = answer[0].strip().lower()
    return default_yes if v == "" else v.startswith(("y", "j"))


def show(commits: list[str]) -> None:
    say("\n".join(commits[:5]) + (f"\n  - ... and {len(commits) - 5} more" if len(commits) > 5 else ""))


# --- installed build ------------------------------------------------------------

STATE_FILE = Path.home() / ".config" / "zotero-mcp" / "installed-build.json"


def repo_version() -> str | None:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "src" / "zotero_mcp" / "_version.py").read_text("utf-8"))
    return m.group(1) if m else None


def target_python() -> list[str]:
    """The Python that Claude Desktop's zotero-mcp.exe belongs to."""
    if os.name == "nt":
        return ["py", "-3.12"]
    return [sys.executable]


def installed_version() -> str | None:
    """Version of the zotero-mcp installed for Claude Desktop, or None if not installed."""
    try:
        out = subprocess.run(
            [*target_python(), "-c", "import zotero_mcp._version as v; print(v.__version__)"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def installed_commit() -> str | None:
    try:
        return json.loads(STATE_FILE.read_text("utf-8")).get("commit")
    except (OSError, ValueError):
        return None


def install_check(interactive: bool) -> None:
    here = repo_version()
    have = installed_version()
    if have is None:
        head("zotero-mcp is not installed for Claude Desktop on this computer (py -3.12).")
        reason = "install"
    elif have != here:
        head(f"Claude Desktop runs zotero-mcp {have}; this folder has {here}.")
        reason = "update"
    else:
        commit = installed_commit()
        head_sha = git("rev-parse", "HEAD")
        if commit and commit != head_sha:
            try:
                changed = lines(git("diff", "--name-only", commit, head_sha, "--", "src", "pyproject.toml"))
            except subprocess.CalledProcessError:
                changed = ["?"]
            if changed:
                head(f"Same version number ({have}), but the code changed since it was installed ({len(changed)} file(s) in src/).")
                reason = "update"
            else:
                good(f"Installed zotero-mcp is up to date ({have})")
                return
        else:
            good(f"Installed zotero-mcp is up to date ({have})")
            return
    if not interactive:
        say("  Run `scripts\\sync.cmd` (or `uv run --no-project scripts/install_build.py`) to build and install it.")
        return
    say("  Close Claude Desktop first (tray icon -> Quit): Windows can't replace zotero-mcp.exe while it runs.")
    if ask("Build and install it now?" if reason == "update" else "Install it now?", False):
        ok = subprocess.run([sys.executable, str(ROOT / "scripts" / "install_build.py")], cwd=ROOT).returncode == 0
        if ok:
            good("Installed. Start Claude Desktop again.")


# --- git ------------------------------------------------------------------------


def check(interactive: bool) -> bool:
    """Compares with GitHub. Returns True when this computer is in step (no commits to pull)."""
    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        upstream = git("rev-parse", "--abbrev-ref", "@{u}")
    except (subprocess.CalledProcessError, FileNotFoundError):
        head("No GitHub branch to compare with (not a git clone, or the branch has no upstream).")
        return True
    try:
        git("fetch", "--quiet", "origin", timeout=20)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        head("Could not reach GitHub (offline, or git is not signed in). Skipped the check for changes from your other computer.")
        return True

    log = lambda rng: lines(git("log", "--format=  - %s (%cr)", rng))  # noqa: E731
    behind = log(f"HEAD..{upstream}")
    ahead = log(f"{upstream}..HEAD")
    dirty = lines(git("status", "--porcelain"))
    changed_here = [line[3:].strip('"').split(" -> ")[-1] for line in dirty]

    if not behind and not ahead and not dirty:
        good(f"In step with GitHub ({branch})")
        return True

    # 1. Changes from the other computer
    if behind:
        head(f"GitHub has {len(behind)} commit(s) this computer doesn't have yet (from your other computer):")
        show(behind)
        changed_there = lines(git("diff", "--name-only", f"HEAD...{upstream}"))
        both = [f for f in changed_here if f in changed_there]
        if both:
            say("  These files were changed here (not committed) and on the other computer:\n    " + "\n    ".join(both))
            say("  Commit them first, then pull (and combine the two versions). Nothing was changed.")
        elif not interactive:
            say("  Run `scripts\\sync.cmd` (or `git pull`) to get them.")
        elif ask("Pull now and put your own new commits on top?" if ahead else "Pull now?", True):
            if git_loud("pull", "--rebase" if ahead else "--ff-only", "--autostash"):
                good("Pulled.")
            else:
                try:
                    git("rebase", "--abort")
                except subprocess.CalledProcessError:
                    pass  # no rebase in progress
                say("  The pull stopped and was undone: both computers changed the same lines. Combine them by hand.")
                return False

    # 2. Commits only on this computer
    still_ahead = log(f"{upstream}..HEAD")
    still_behind = lines(git("log", "--oneline", f"HEAD..{upstream}"))
    if still_ahead:
        head(f"This computer has {len(still_ahead)} commit(s) that are not on GitHub (your other computer doesn't have them):")
        show(still_ahead)
        if still_behind:
            say("  Pull first (see above), then push.")
        elif not interactive:
            say("  Run `scripts\\sync.cmd` (or `git push`) to send them.")
        elif ask("Push now?", True):
            if git_loud("push"):
                good("Pushed.")

    # 3. Uncommitted changes
    if dirty:
        head(f"{len(dirty)} changed file(s) are not committed yet; they exist only on this computer until you commit and push.")

    return not still_behind


def main() -> int:
    interactive = sys.stdin.isatty() and "--check" not in sys.argv
    say(f"zotero-mcp: {ROOT}")
    in_step = check(interactive)
    if "--no-install" in sys.argv:
        pass
    elif in_step:
        install_check(interactive)
    else:
        say("  (The installed zotero-mcp is checked once this computer has the commits from GitHub.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
