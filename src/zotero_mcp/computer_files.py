"""Records in a folder that several computers sync (OneDrive), one file per computer.

A sync service moves whole files. When two computers change the same file, one change is lost
or a conflict copy appears beside it. So in a shared ``state_dir`` each computer writes only
its own copy, ``name-COMPUTER.ext``, and every reader merges all copies: the old shared file,
the other computers' copies, conflict copies, and this computer's own copy last.

Merging works per entry. An entry (a record with plain values, such as one item's fetch state)
gets an ``_updated`` time when a computer changes it; the newest entry wins. Groups of entries
(``{"papers": {...}}``) are merged entry by entry. Dates kept as plain values (a watermark like
``last_new_items``) keep the later one.

Without a shared folder (the default) nothing changes: one file, as before.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
STAMP = "_updated"


def computer() -> str:
    from zotero_mcp.embedding_store import computer_name

    return computer_name()


def is_shared(path: Path) -> bool:
    """True when ``path`` lies in a state folder other computers sync."""
    from zotero_mcp import fulltext_fetch as ff

    try:
        shared = ff.shared_dir().resolve()
        if shared == ff.config_dir().resolve():
            return False
        path.resolve().relative_to(shared)
        return True
    except (ValueError, OSError):
        return False


def own_path(path: Path) -> Path:
    """The copy this computer writes: ``state-DESKTOP-A.json`` for ``state.json``."""
    if not is_shared(path):
        return path
    return path.with_name(f"{path.stem}-{computer()}{path.suffix}")


def all_paths(path: Path) -> list[Path]:
    """Every existing copy, the old shared file first and this computer's own copy last."""
    own = own_path(path)
    if own == path:
        return [path] if path.is_file() else []
    others = sorted(p for p in path.parent.glob(f"{path.stem}-*{path.suffix}")
                    if p != own and not p.name.startswith(".") and p.is_file())
    return [p for p in (path, *others, own) if p.is_file()]


# -- merging -------------------------------------------------------------------------------

def _is_entry(value: Any) -> bool:
    return isinstance(value, dict) and any(not isinstance(v, dict) for v in value.values())


def _is_group(value: Any) -> bool:
    return isinstance(value, dict) and not _is_entry(value)


def _when(entry: dict) -> str:
    """When an entry was last changed: its stamp, or else its latest date."""
    stamp = entry.get(STAMP)
    if isinstance(stamp, str) and stamp:
        return stamp
    dates = [v for v in entry.values() if isinstance(v, str) and _DATE.match(v)]
    return max(dates, default="")


def merge(older: Any, newer: Any) -> Any:
    """``newer`` comes from a later copy in the reading order; on a tie it wins."""
    if _is_group(older) and _is_group(newer):
        out = dict(older)
        for key, value in newer.items():
            out[key] = merge(out[key], value) if key in out else value
        return out
    if _is_entry(older) and _is_entry(newer):
        return newer if _when(newer) >= _when(older) else older
    if isinstance(older, dict) and isinstance(newer, dict):
        # A record that holds plain values beside groups (``{"last_new_items": ..., "checked": {...}}``).
        out = dict(older)
        for key, value in newer.items():
            out[key] = merge(out[key], value) if key in out else value
        return out
    if isinstance(older, str) and isinstance(newer, str) and _DATE.match(older) and _DATE.match(newer):
        return max(older, newer)
    return newer


# -- reading and writing ---------------------------------------------------------------------

class Record(dict):
    """A merged record that remembers what it looked like when read, so a save can tell which
    entries this computer changed."""

    _before: Any = None


def load(path: Path, default: Callable[[], Any] = dict) -> Any:
    merged: Any = default()
    for p in all_paths(path):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        merged = merge(merged, data) if merged else data
    if isinstance(merged, dict):
        record = Record(merged)
        record._before = json.loads(json.dumps(merged))
        return record
    return merged


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != STAMP}
    return value


def _stamp_changes(before: Any, after: dict, now: str) -> None:
    for key, value in after.items():
        old = before.get(key) if isinstance(before, dict) else None
        if _is_entry(value):
            if _strip(old) != _strip(value):
                value[STAMP] = now
        elif isinstance(value, dict):
            _stamp_changes(old if isinstance(old, dict) else {}, value, now)


def save(path: Path, data: dict, **dump_kw: Any) -> Path:
    """Write ``data`` (a merged view, normally from ``load``) to this computer's own copy, in one
    step. Entries changed since ``load`` get a new stamp."""
    from zotero_mcp.fulltext_fetch import write_json

    target = own_path(path)
    if target != path:
        before = getattr(data, "_before", None)
        if before is not None:
            _stamp_changes(before, data, _dt.datetime.now().isoformat(timespec="microseconds"))
    if dump_kw.get("separators"):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_text(json.dumps(dict(data), ensure_ascii=False, **dump_kw), encoding="utf-8")
        tmp.replace(target)
    else:
        write_json(target, dict(data), indent=dump_kw.get("indent", 1))
    if isinstance(data, Record):
        data._before = json.loads(json.dumps(data))
    return target


def read_lines(path: Path) -> list[str]:
    """All copies of an append-only log, in file order (sort by time where it matters)."""
    lines: list[str] = []
    for p in all_paths(path):
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except OSError:
            continue
    return lines


def sum_counts(path: Path) -> dict:
    """Counters (``{"service": {"2026-10": 12}}``) added up over every copy except this
    computer's own: what the other computers (and the old shared file) have used."""
    own = own_path(path)
    total: dict = {}
    for p in all_paths(path):
        if p == own:
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for service, periods in (data or {}).items():
            if not isinstance(periods, dict):
                continue
            bucket = total.setdefault(service, {})
            for period, n in periods.items():
                try:
                    bucket[period] = int(bucket.get(period, 0)) + int(n)
                except (TypeError, ValueError):
                    pass
    return total
