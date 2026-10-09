"""The review window shows only what a suggestion changes."""

from zotero_mcp.fulltext_window import describe_change, diff_segments


def test_only_the_change_is_shown():
    yours = "Aelterman, Nathalie; Vansteenkiste, Maarten; Keer, Hilde; Haerens, Leen"
    theirs = "Aelterman, Nathalie; Vansteenkiste, Maarten; Van Keer, Hilde; Haerens, Leen"
    assert describe_change("creators", yours, theirs) == "Keer, Hilde → Van Keer, Hilde"
    assert describe_change("creators", "A, B; C, D", "A, B; C, D; E, F") == "+ E, F"
    assert describe_change("year", "2023", "2025") == "2023 → 2025"
    assert describe_change("title", "Playground designs to increase physical activity during recess",
                           "Playground designs to increase physical activity during recess: A systematic review"
                           ) == '+ ": A systematic review"'
    assert describe_change("title", "The colour of motivation in physical education today",
                           "The color of motivation in physical education today") == '"colour" → "color"'
    assert describe_change("bookTitle", "", "Advances in Child Development") == "add Advances in Child Development"


def test_both_values_are_marked_where_they_differ():
    left, right = diff_segments("creators", "A, B; Keer, Hilde", "A, B; Van Keer, Hilde")
    assert [t for t, d in left if d] == ["Keer, Hilde"] and [t for t, d in right if d] == ["Van Keer, Hilde"]
    left, right = diff_segments("pages", "195-206", "194-206")
    assert "".join(t for t, _ in left) == "195-206" and [t for t, d in right if d] == ["194"]
