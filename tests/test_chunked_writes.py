"""Long item lists in write tools run in chunks and stop before the bridge gives up (arne.71)."""
import itertools

from zotero_mcp.tools import write


def test_small_list_runs_once():
    calls = []
    out = write._run_in_chunks(["A", "B"], lambda c: calls.append(c) or "ok", "tool")
    assert calls == [["A", "B"]] and out == "ok"


def test_chunks_of_ten():
    keys = [f"K{i}" for i in range(25)]
    calls = []
    out = write._run_in_chunks(keys, lambda c: calls.append(c) or f"did {len(c)}", "tool")
    assert [len(c) for c in calls] == [10, 10, 5]
    assert "Stopped" not in out


def test_stops_after_budget_and_lists_the_rest(monkeypatch):
    clock = itertools.count(0, 20)  # every chunk "takes" 20 s
    monkeypatch.setattr(write._time, "monotonic", lambda: next(clock))
    keys = [f"K{i}" for i in range(40)]
    out = write._run_in_chunks(keys, lambda c: "ok", "zotero_set_item_collections")
    assert "Stopped after 20 of 40 items" in out
    assert '"K20"' in out and '"K39"' in out and '"K19"' not in out
