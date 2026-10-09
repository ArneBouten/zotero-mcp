"""Share the records of the checks between computers (``zotero-mcp share-state FOLDER``).

The records of what was checked, searched and decided live in ``~/.config/zotero-mcp`` by
default, so a second computer would check everything again and bring back rejected
suggestions. This moves them to a folder both computers sync (OneDrive) and points
``"state_dir"`` in config.json at it. Run it on each computer with the same folder: the first
one copies its records there, the next ones use what is there. The local files stay as they
are, as a backup.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from collections.abc import Callable
from pathlib import Path

#: What is shared. The search index, its labels, the fetcher's Chrome profile, keys and locks
#: stay on each computer.
SHARED = ("maintenance.json", "citations.json", "metadata", "fulltext")
#: Inside those, what stays local: a background fetch's own log.
_LOCAL_ONLY = ("bg-*",)


def _portable(folder: Path) -> str:
    """The folder as config.json keeps it: under the home folder as ``~/...``, so the same line
    works on a computer with another user name."""
    home = Path.home()
    try:
        return "~/" + folder.resolve().relative_to(home.resolve()).as_posix()
    except ValueError:
        return str(folder)


def share(folder: str | Path, log: Callable[[str], None] = print, config_dir: Path | None = None) -> dict:
    from zotero_mcp import fulltext_fetch as ff

    local = config_dir or ff.config_dir()
    target = Path(str(folder)).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    copied, kept = [], []
    for name in SHARED:
        src, dst = local / name, target / name
        if dst.exists():
            kept.append(name)           # another computer shared it already: that one counts
            continue
        if not src.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_LOCAL_ONLY))
        else:
            shutil.copy2(src, dst)
        copied.append(name)

    config = local / "config.json"
    data: dict = {}
    if config.exists():
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(config, config.with_name(f"config.json.bak-{stamp}"))
        data = json.loads(config.read_text(encoding="utf-8"))
    data["state_dir"] = _portable(target)
    config.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    if copied:
        log(f"Copied this computer's records to {target}: {', '.join(copied)}.")
    if kept:
        log(f"Already shared from another computer, used as they are: {', '.join(kept)}.")
    log(f'config.json now has "state_dir": "{data["state_dir"]}" (backup beside it). '
        "The records in .config\\zotero-mcp stay as a backup and are no longer used.")
    return {"copied": copied, "kept": kept, "state_dir": data["state_dir"]}
