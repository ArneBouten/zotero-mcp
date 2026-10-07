"""Inspecting and editing citations in a Word document (zotero_mcp.word_edit).

Documents are built with the same minimal WordprocessingML helpers as the
marker tests, converted with word_citations first where they need live
citations, so the fields under test are exactly what the conversion writes.
"""

import zipfile

import pytest

etree = pytest.importorskip("lxml.etree")

from test_word_citations import (  # noqa: E402
    LIBRARY,
    fields,
    make_docx,
    para,
    read,
    resolver,
    run,
    visible_text,
)

from zotero_mcp import word_citations as wc  # noqa: E402
from zotero_mcp import word_edit as we  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
HEADING = '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
CORE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:creator>Someone Else</dc:creator>'
    "<cp:lastModifiedBy>Arne Bouten</cp:lastModifiedBy></cp:coreProperties>"
)


def manuscript(tmp_path, name="m.docx"):
    """Two live citations, a plain-text one, and a typed reference list."""
    src = make_docx(
        tmp_path / name,
        para(run("Control matters [@SOEN2009] and (Smith, 2020) said so; Bion (1962) agreed [@GROL2009].")),
        para(run("References"), ppr=HEADING),
        para(run("Smith, J. (2020). A paper.")),
        para(run("Bion, W. (1962). Learning from experience.")),
        extra_files={"docProps/core.xml": CORE},
    )
    wc.convert_docx(src, resolver(), in_place=True)
    (tmp_path / f"{src.stem}.bak.docx").unlink()
    return src


def accepted_text(xml: str) -> str:
    """Visible text once every tracked change is accepted."""
    root = etree.fromstring(xml.encode())
    for d in list(root.iter(f"{W}del")):
        parent = d.getparent()
        if parent.tag == f"{W}rPr":  # a deleted paragraph mark: drop the paragraph
            p = parent.getparent().getparent()
            p.getparent().remove(p)
        elif parent is not None:
            parent.remove(d)
    texts = []
    for p in root.iter(f"{W}p"):
        texts.append("".join(t.text or "" for t in p.iter(f"{W}t")))
    return "\n".join(texts)


def plain_text(xml: str) -> str:
    root = etree.fromstring(xml.encode())
    return "\n".join("".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p"))


# --- inspect ---------------------------------------------------------------------------


def test_inspect_lists_citations_plain_citations_and_reference_lists(tmp_path):
    inv = we.inspect_docx(manuscript(tmp_path))
    assert [c.id for c in inv.citations] == ["C1", "C2"]
    c1 = inv.citations[0]
    assert c1.text == "(Soenens et al., 2009)" and c1.paragraph == "D1"
    assert [it.key for it in c1.items] == ["SOEN2009"]
    assert "Control matters (Soenens et al., 2009)" in c1.context
    assert not c1.hand_edited
    # Plain-text citations are found; text inside Zotero fields is not.
    assert [(p.id, p.text) for p in inv.plain_citations] == [("P1", "(Smith, 2020)"), ("P2", "Bion (1962)")]
    assert len(inv.reference_lists) == 1
    rl = inv.reference_lists[0]
    assert rl.heading == "D2" and [e[0] for e in rl.entries] == ["D3", "D4"]
    assert inv.style == "apa" and not inv.markers


def test_inspect_reports_locators_markers_and_hand_edits(tmp_path):
    src = make_docx(tmp_path / "x.docx", para(run("A [@SOEN2009, p. 12] and B [-@BION1962].")),
                    para(run("Still to convert [@GROL2009].")))
    wc.convert_docx(src, resolver(calls=None), output_path=tmp_path / "y.docx")
    out = tmp_path / "y.docx"
    # Simulate a hand edit in Word: the visible text no longer matches.
    with zipfile.ZipFile(out) as z:
        files = {n: z.read(n) for n in z.namelist()}
    files["word/document.xml"] = files["word/document.xml"].replace(b">(1962)<", b">(1962, edited)<")
    with zipfile.ZipFile(out, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    inv = we.inspect_docx(out)
    first = inv.citations[0].items[0]
    assert (first.locator, first.locator_label) == ("12", "page")
    assert inv.citations[1].items[0].suppress_author
    assert inv.citations[1].hand_edited
    assert inv.markers == [] and len(inv.citations) == 3


def test_inspect_footnotes_and_bibliography(tmp_path):
    footnotes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:footnotes xmlns:w="{wc.W_NS}"><w:footnote w:id="1">'
        + para(run("See [@BION1962].")) + "</w:footnote></w:footnotes>"
    )
    src = make_docx(tmp_path / "f.docx", para(run("Body [@SOEN2009].")), para(run("{{bibliography}}")),
                    extra_files={"word/footnotes.xml": footnotes})
    wc.convert_docx(src, resolver(), in_place=True)
    inv = we.inspect_docx(src)
    assert [(c.id, c.paragraph) for c in inv.citations] == [("C1", "D1"), ("C2", "F1")]
    assert len(inv.bibliographies) == 1 and inv.bibliographies[0].id == "B1"
    assert inv.reference_lists == []


def test_inspect_rejects_non_docx(tmp_path):
    bad = tmp_path / "a.txt"
    bad.write_text("x")
    with pytest.raises(wc.WordCitationError):
        we.inspect_docx(bad)
    with pytest.raises(wc.WordCitationError):
        we.inspect_docx(tmp_path / "missing.docx")


# --- write modes ------------------------------------------------------------------------


EDITS = [
    {"op": "replace_citation", "citation": "C1", "marker": "[@SOEN2009; @GROL2009, p. 4]"},
    {"op": "delete_citation", "citation": "C2"},
    {"op": "replace_text", "paragraph": "D1", "find": "(Smith, 2020)", "with": "[@BION1962]"},
    {"op": "comment", "citation": "C1", "text": "Check: adults only?"},
    {"op": "replace_reference_list", "reference_list": "R1"},
]
EXPECTED_FIRST = ("Control matters (Soenens et al., 2009; Grolnick & Pomerantz, 2009, p. 4) "
                  "and (Bion, 1962) said so; Bion (1962) agreed.")


def test_write_mode_is_required(tmp_path):
    src = manuscript(tmp_path)
    with pytest.raises(wc.WordCitationError, match="ask the user"):
        we.edit_docx(src, resolver(), write_mode="", edits=EDITS)


def test_new_file_leaves_the_original(tmp_path):
    src = manuscript(tmp_path)
    before = read(src)
    report = we.edit_docx(src, resolver(), write_mode="new_file", edits=EDITS)
    assert read(src) == before
    out = tmp_path / "m (Zotero).docx"
    assert report.output_path == str(out) and not report.backup_path
    assert [r.status for r in report.results] == ["done"] * 5
    text = plain_text(read(out))
    assert text.splitlines()[0] == EXPECTED_FIRST
    assert "Smith, J. (2020)" not in text  # the typed list is gone
    assert not (tmp_path / we.BACKUP_DIR).exists()


def test_overwrite_keeps_a_backup(tmp_path):
    src = manuscript(tmp_path)
    before = src.read_bytes()
    report = we.edit_docx(src, resolver(), write_mode="overwrite", edits=EDITS)
    backups = list((tmp_path / we.BACKUP_DIR).glob("m *.docx"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    assert report.backup_path == str(backups[0])
    assert plain_text(read(src)).splitlines()[0] == EXPECTED_FIRST
    assert "<w:ins " not in read(src) and "<w:del " not in read(src)


def test_tracked_changes_accept_to_the_same_text(tmp_path):
    src = manuscript(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="tracked_changes", edits=EDITS)
    xml = read(src)
    assert report.author == "Arne Bouten"  # lastModifiedBy, not dc:creator
    assert 'w:author="Arne Bouten"' in xml
    assert "<w:delText" in xml and "<w:delInstrText" in xml
    accepted = accepted_text(xml)
    assert accepted.splitlines()[0] == EXPECTED_FIRST
    assert "Smith, J. (2020)" not in accepted
    # Rejecting instead keeps the original wording visible as deleted text.
    root = etree.fromstring(xml.encode())
    deleted = "".join(t.text or "" for t in root.iter(f"{W}delText"))
    assert "(Smith, 2020)" in deleted and "Smith, J. (2020). A paper." in deleted
    # Revision ids are unique.
    ids = [el.get(f"{W}id") for el in root.iter(f"{W}ins", f"{W}del")]
    assert len(ids) == len(set(ids))


def test_tracked_paragraph_marks_put_revision_first(tmp_path):
    src = manuscript(tmp_path)
    we.edit_docx(src, resolver(), write_mode="tracked_changes",
                 edits=[{"op": "replace_reference_list", "reference_list": "R1"}])
    root = etree.fromstring(read(src).encode())
    for rpr in root.iter(f"{W}rPr"):
        if rpr.getparent().tag == f"{W}pPr":
            assert rpr[0].tag in (f"{W}ins", f"{W}del")


def test_refuses_a_document_open_in_word(tmp_path):
    src = manuscript(tmp_path)
    (tmp_path / "~$m.docx").write_text("lock")
    with pytest.raises(wc.WordCitationError, match="open in Word"):
        we.edit_docx(src, resolver(), write_mode="overwrite", edits=EDITS)


def test_dry_run_writes_nothing(tmp_path):
    src = manuscript(tmp_path)
    before = src.read_bytes()
    report = we.edit_docx(src, resolver(), write_mode="overwrite", edits=EDITS, dry_run=True)
    assert src.read_bytes() == before and report.dry_run
    assert not (tmp_path / we.BACKUP_DIR).exists()
    assert all(r.status == "done" for r in report.results)


# --- individual edits -------------------------------------------------------------------


def test_delete_citation_removes_the_space_before_punctuation(tmp_path):
    src = manuscript(tmp_path)
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "delete_citation", "citation": "C2"}])
    assert plain_text(read(src)).splitlines()[0].endswith("Bion (1962) agreed.")
    assert len(fields(read(src))) == 1


def test_expect_guard_and_bad_targets_are_skipped(tmp_path):
    src = manuscript(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="new_file", edits=[
        {"op": "delete_citation", "citation": "C1", "expect": "(Smith, 2020)"},
        {"op": "delete_citation", "citation": "C9"},
        {"op": "replace_citation", "citation": "C2", "marker": "[@NOTINLIB]"},
        {"op": "replace_citation", "citation": "C2", "marker": "not a marker"},
        {"op": "replace_text", "paragraph": "D1", "find": "absent words", "with": "x"},
        {"op": "rebuild_bibliography", "bibliography": "B1"},
    ])
    assert [r.status for r in report.results] == ["skipped"] * 6
    assert "NOTINLIB" in report.unresolved
    assert not (tmp_path / "m (Zotero).docx").exists()  # nothing changed, nothing written


def test_unknown_op_is_an_error(tmp_path):
    with pytest.raises(wc.WordCitationError, match="Unknown edit op"):
        we.edit_docx(manuscript(tmp_path), resolver(), write_mode="new_file", edits=[{"op": "explode"}])


def test_edits_may_arrive_as_json_text(tmp_path):
    src = manuscript(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="new_file",
                          edits='[{"op": "delete_citation", "citation": "C1"}]')
    assert report.results[0].status == "done"


def test_replace_text_cannot_cut_into_a_citation(tmp_path):
    src = manuscript(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="new_file", edits=[
        {"op": "replace_text", "paragraph": "D1", "find": "et al., 2009", "with": "x"},
    ])
    assert report.results[0].status == "skipped"
    assert "citation" in report.results[0].detail


def test_replace_text_second_occurrence(tmp_path):
    src = make_docx(tmp_path / "o.docx", para(run("the cat and the cat")))
    we.edit_docx(src, resolver(), write_mode="overwrite",
                 edits=[{"op": "replace_text", "paragraph": "D1", "find": "cat", "with": "dog", "occurrence": 2}])
    assert plain_text(read(src)) == "the cat and the dog"


def test_comments_are_registered_and_linked(tmp_path):
    src = manuscript(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="new_file", edits=[
        {"op": "comment", "citation": "C2", "text": "Does Grolnick say this?"},
        {"op": "comment", "paragraph": "D1", "find": "Bion (1962)", "text": "Typed by hand."},
        {"op": "comment", "paragraph": "D3", "text": "Whole entry."},
    ])
    out = tmp_path / "m (Zotero).docx"
    assert report.comments == 3
    doc = read(out)
    comments = read(out, "word/comments.xml")
    ct = read(out, "[Content_Types].xml")
    rels = read(out, "word/_rels/document.xml.rels")
    assert "/word/comments.xml" in ct and "relationships/comments" in rels
    croot = etree.fromstring(comments.encode())
    ids = [c.get(f"{W}id") for c in croot.iter(f"{W}comment")]
    assert len(ids) == 3
    for cid in ids:
        assert f'commentRangeStart w:id="{cid}"' in doc
        assert f'commentRangeEnd w:id="{cid}"' in doc
        assert f'commentReference w:id="{cid}"' in doc
    assert "Does Grolnick say this?" in comments
    # The text around the comment is intact.
    assert plain_text(doc).splitlines()[0].startswith("Control matters (Soenens et al., 2009) and (Smith, 2020)")


def test_comment_and_replacement_on_the_same_citation(tmp_path):
    src = manuscript(tmp_path)
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[
        {"op": "comment", "citation": "C1", "text": "Swapped the source."},
        {"op": "replace_citation", "citation": "C1", "marker": "[@BION1962]"},
    ])
    doc = read(src)
    start = doc.index("commentRangeStart")
    end = doc.index("commentRangeEnd")
    assert "(Bion, 1962)" in doc[start:end]


def test_rebuild_and_delete_bibliography(tmp_path):
    src = make_docx(tmp_path / "b.docx", para(run("A [@SOEN2009].")), para(run("{{bibliography}}")))
    wc.convert_docx(src, resolver(), in_place=True)
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[
        {"op": "replace_text", "paragraph": "D1", "find": "A ", "with": "A [@BION1962] "},
        {"op": "rebuild_bibliography", "bibliography": "B1"},
    ])
    bib = [f for f in fields(read(src)) if "ZOTERO_BIBL" in f[0]]
    assert len(bib) == 1 and "Bion" in bib[0][1] and "Soenens" in bib[0][1]
    assert bib[0][1].index("Bion") < bib[0][1].index("Soenens")  # alphabetical
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "delete_bibliography", "bibliography": "B1"}])
    assert not [f for f in fields(read(src)) if "ZOTERO_BIBL" in f[0]]
    assert plain_text(read(src)).strip() == "A (Bion, 1962) (Soenens et al., 2009)."


def test_insert_bibliography_at_the_end_and_after_a_paragraph(tmp_path):
    src = make_docx(tmp_path / "i.docx", para(run("A [@SOEN2009].")), para(run("Closing words.")))
    wc.convert_docx(src, resolver(), in_place=True)
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "insert_bibliography"}])
    xml = read(src)
    assert plain_text(xml).splitlines()[-1].startswith("Soenens et al., 2009")
    assert xml.index("ZOTERO_BIBL") < xml.index("<w:sectPr")
    src2 = make_docx(tmp_path / "j.docx", para(run("A [@SOEN2009].")), para(run("Closing words.")))
    we.edit_docx(src2, resolver(), write_mode="overwrite", edits=[{"op": "insert_bibliography", "after": "D1"}])
    lines = plain_text(read(src2)).splitlines()
    assert lines[1].startswith("Soenens et al., 2009") and lines[-1] == "Closing words."


def test_markers_and_bibliography_placeholder_are_converted_tracked(tmp_path):
    src = make_docx(tmp_path / "t.docx", para(run("New claim [@SOEN2009]"), run(" and more.", bold=True)),
                    para(run("{{bibliography}}")), extra_files={"docProps/core.xml": CORE})
    report = we.edit_docx(src, resolver(), write_mode="tracked_changes")
    assert report.markers_converted == 1 and report.bibliography_written and report.prefs_written
    xml = read(src)
    text = accepted_text(xml)
    assert text.splitlines()[0] == "New claim (Soenens et al., 2009) and more."
    assert "{{bibliography}}" not in text and "Soenens et al., 2009. Title SOEN2009." in text


def test_footnote_citation_can_be_replaced(tmp_path):
    footnotes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:footnotes xmlns:w="{wc.W_NS}"><w:footnote w:id="1">'
        + para(run("See [@BION1962].")) + "</w:footnote></w:footnotes>"
    )
    src = make_docx(tmp_path / "f.docx", para(run("Body.")), extra_files={"word/footnotes.xml": footnotes})
    wc.convert_docx(src, resolver(), in_place=True)
    we.edit_docx(src, resolver(), write_mode="overwrite",
                 edits=[{"op": "replace_citation", "citation": "C1", "marker": "[@GROL2009]"}])
    assert "Grolnick" in visible_text(read(src, "word/footnotes.xml"))


def test_untouched_parts_survive(tmp_path):
    src = manuscript(tmp_path)
    with zipfile.ZipFile(src) as z:
        before = {n: z.read(n) for n in z.namelist()}
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "delete_citation", "citation": "C1"}])
    with zipfile.ZipFile(src) as z:
        after = {n: z.read(n) for n in z.namelist()}
    for name in ("docProps/core.xml", "_rels/.rels", "docProps/custom.xml"):
        assert after[name] == before[name]


def test_python_docx_opens_every_mode(tmp_path):
    docx = pytest.importorskip("docx")
    for mode in we.WRITE_MODES:
        src = manuscript(tmp_path, f"{mode}.docx")
        report = we.edit_docx(src, resolver(), write_mode=mode, edits=EDITS)
        docx.Document(report.output_path)  # raises on a malformed package


def test_library_fixture_still_resolves():
    # Guards the shared fixture this file relies on.
    assert set(LIBRARY) >= {"SOEN2009", "GROL2009", "BION1962"}


def test_a_field_sharing_a_run_with_text_keeps_that_text(tmp_path):
    # Some documents hold a whole field and the words around it in one run.
    code = wc.citation_field_code(wc.find_clusters("[@SOEN2009]")[0], LIBRARY, "(Soenens et al., 2009)")
    code = code.replace("&", "&amp;").replace("<", "&lt;")
    one_run = (
        '<w:r><w:t xml:space="preserve">Before </w:t><w:fldChar w:fldCharType="begin"/>'
        f'<w:instrText xml:space="preserve">{code}</w:instrText><w:fldChar w:fldCharType="separate"/>'
        '<w:t>(Soenens et al., 2009)</w:t><w:fldChar w:fldCharType="end"/>'
        '<w:t xml:space="preserve"> and after.</w:t></w:r>'
    )
    src = make_docx(tmp_path / "r.docx", f"<w:p>{one_run}</w:p>")
    inv = we.inspect_docx(src)
    assert [c.text for c in inv.citations] == ["(Soenens et al., 2009)"]
    we.edit_docx(src, resolver(), write_mode="overwrite",
                 edits=[{"op": "replace_citation", "citation": "C1", "marker": "[@BION1962]"}])
    assert plain_text(read(src)) == "Before (Bion, 1962) and after."
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "delete_citation", "citation": "C1"}])
    assert plain_text(read(src)) == "Before and after."


# --- shared documents: items from other libraries ------------------------------------


def _shared_doc(tmp_path):
    """Soenens cited from a co-author's group, then from this library too."""
    theirs = wc.ResolvedItem(
        key="ZZZZ0001", uri="http://zotero.org/groups/2547418/items/ZZZZ0001",
        item_data={"id": 5, "type": "article-journal", "title": "Title SOEN2009", "DOI": "10.1/abc",
                   "author": [{"family": "Soenens"}, {"family": "Vansteenkiste"}, {"family": "Luyten"}],
                   "container-title": "J. Pers.", "issued": {"date-parts": [["2009"]]}},
        citation="(Soenens et al., 2009)", bibliography="Soenens et al., 2009. Title SOEN2009.",
    )
    mine = wc.ResolvedItem(**{**LIBRARY["SOEN2009"].__dict__})
    mine.item_data = {**mine.item_data, "DOI": "https://doi.org/10.1/ABC"}
    src = make_docx(tmp_path / "s.docx", para(run("Theirs [@T] and mine [@M].")), para(run("{{bibliography}}")))
    wc.convert_docx(src, lambda keys: {"T": theirs, "M": mine}, in_place=True)
    return src, mine


def test_inspect_reports_libraries_and_duplicates(tmp_path):
    src, _ = _shared_doc(tmp_path)
    inv = we.inspect_docx(src)
    assert inv.libraries == {"groups/2547418": 1, "users/7756991": 1}
    assert len(inv.duplicates) == 1
    assert [cid for cid, _ in inv.duplicates[0]] == ["C1", "C2"]


def test_reusing_a_cited_item_avoids_a_duplicate(tmp_path):
    src, mine = _shared_doc(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="overwrite", edits=[
        {"op": "replace_citation", "citation": "C2", "marker": "[@C1, p. 5]"},
        {"op": "rebuild_bibliography", "bibliography": "B1"},
    ])
    assert [r.status for r in report.results] == ["done", "done"]
    inv = we.inspect_docx(src)
    assert inv.duplicates == [] and inv.libraries == {"groups/2547418": 2}
    assert inv.citations[1].text == "(Soenens et al., 2009, p. 5)"
    bib = [f for f in fields(read(src)) if "ZOTERO_BIBL" in f[0]][0][1]
    assert bib.count("Soenens") == 1  # the co-author's item, from the copy in the citation


def test_new_citation_of_a_work_cited_from_elsewhere_reuses_that_item(tmp_path):
    src, mine = _shared_doc(tmp_path)
    lib = {"NEWKEY01": wc.ResolvedItem(**{**mine.__dict__, "key": "NEWKEY01",
                                          "uri": "http://zotero.org/users/7756991/items/NEWKEY01"})}
    edits = [{"op": "replace_text", "paragraph": "D1", "find": "Theirs ", "with": "Theirs [@NEWKEY01] "}]
    report = we.edit_docx(src, lambda keys: {k: lib[k] for k in keys if k in lib},
                          write_mode="new_file", edits=edits)
    assert any("already cited in C1" in n for n in report.notes) and not report.warnings
    inv = we.inspect_docx(tmp_path / "s (Zotero).docx")
    assert inv.citations[0].items[0].uri.startswith("http://zotero.org/groups/2547418/")
    # Without reuse, the same edit only warns.
    report = we.edit_docx(src, lambda keys: {k: lib[k] for k in keys if k in lib}, write_mode="new_file",
                          edits=edits, output_path=tmp_path / "w.docx", reuse_cited=False)
    assert any("[@C1]" in w for w in report.warnings)


def test_merge_duplicates_points_each_work_to_one_item(tmp_path):
    src, _ = _shared_doc(tmp_path)
    before = plain_text(read(src))
    report = we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "merge_duplicates"}])
    assert report.results[0].status == "done"
    inv = we.inspect_docx(src)
    assert inv.duplicates == [] and list(inv.libraries) == ["groups/2547418"]
    assert plain_text(read(src)) == before  # only the links changed
    assert not any(c.hand_edited for c in inv.citations)


def test_merge_duplicates_keeps_the_chosen_item(tmp_path):
    src, _ = _shared_doc(tmp_path)
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "merge_duplicates", "keep": "C2"}])
    assert list(we.inspect_docx(src).libraries) == ["users/7756991"]
    report = we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "merge_duplicates"}])
    assert report.results[0].status == "skipped"


def test_merge_collapses_a_work_cited_twice_in_one_citation(tmp_path):
    src, mine = _shared_doc(tmp_path)
    we.edit_docx(src, resolver(), write_mode="overwrite", reuse_cited=False,
                 edits=[{"op": "replace_citation", "citation": "C2", "marker": "[@C1; @SOEN2009]"}])
    we.edit_docx(src, resolver(), write_mode="overwrite", edits=[{"op": "merge_duplicates"}])
    inv = we.inspect_docx(src)
    assert [len(c.items) for c in inv.citations] == [1, 1]


def test_bibliography_keeps_items_from_other_libraries(tmp_path):
    src, _ = _shared_doc(tmp_path)
    report = we.edit_docx(src, lambda keys: {}, write_mode="overwrite",
                          edits=[{"op": "rebuild_bibliography", "bibliography": "B1"}])
    assert report.unresolved == []  # already cited: rendered from the stored copy
    bib = [f for f in fields(read(src)) if "ZOTERO_BIBL" in f[0]][0][1]
    # Two different items, so two entries, as Zotero itself would list them.
    assert "Soenens et al., 2009. Title SOEN2009. J. Pers." in bib and "Title SOEN2009, 2009." in bib


def test_reuse_key_out_of_range_is_unresolved(tmp_path):
    src, _ = _shared_doc(tmp_path)
    report = we.edit_docx(src, resolver(), write_mode="new_file",
                          edits=[{"op": "replace_citation", "citation": "C2", "marker": "[@C1.3]"}])
    assert report.results[0].status == "skipped" and "C1.3" in report.unresolved


def test_author_skips_assistant_names(tmp_path, monkeypatch):
    core = CORE.replace("Arne Bouten", "Claude")
    src = make_docx(tmp_path / "a.docx", para(run("x [@SOEN2009]")), extra_files={"docProps/core.xml": core})
    monkeypatch.delenv(we.AUTHOR_ENV, raising=False)
    assert we.edit_docx(src, resolver(), write_mode="tracked_changes").author == "Someone Else"
    monkeypatch.setenv(we.AUTHOR_ENV, "Arne Bouten")
    src2 = make_docx(tmp_path / "b.docx", para(run("x [@SOEN2009]")), extra_files={"docProps/core.xml": core})
    assert we.edit_docx(src2, resolver(), write_mode="tracked_changes").author == "Arne Bouten"
