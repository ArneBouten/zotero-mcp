"""Printed page numbers, headings and reference lists for indexed passages."""

import json

import pytest

from zotero_mcp import structure as st


def test_section_names_in_several_languages_and_combinations():
    cases = {
        "2.3. Statistical analyses": "Methods", "Materials and methods": "Methods",
        "Analyses and Results": "Results", "Results and discussion": "Results",
        "Discussion and conclusions": "Discussion", "Inleiding": "Introduction", "Résultats": "Results",
        "Literatur": "References", "Acknowledgements": "Back matter", "Kestrel surveys": None, "Study 2": None,
        "Introdução": "Introduction", "Discussão": "Discussion", "Considerações finais": "Conclusion",
        "Referências": "References", "Risultati": "Results", "Metodi": "Methods", "Conclusioni": "Conclusion",
    }
    for heading, label in cases.items():
        assert st.canonical_section(heading) == label, heading


def test_roman_numerals():
    assert st.roman_value("xiv") == 14 and st.roman_value("iiii") is None and st.to_roman(9) == "ix"


def _scan(n, margins, **kw):
    return {"pages": n, "labels": None, "toc": [], "margins": margins, "candidates": [], **kw}


def test_printed_numbers_by_majority_with_an_unnumbered_first_page():
    # An article printed as pages 377-382: the first page has no footer number,
    # one page has a stray year in the footer.
    margins = [[]] + [[[377 + i, "a"]] for i in range(1, 6)]
    margins[3].append([2017, "a"])
    labels, how = st.page_labels(_scan(6, margins))
    assert how == "printed numbers" and labels == ["377", "378", "379", "380", "381", "382"]


def test_front_matter_in_roman_then_arabic():
    margins = [[], [[2, "r"]], [[3, "r"]], [[4, "r"]], [[1, "a"]], [[2, "a"]], [[3, "a"]], [[4, "a"]]]
    labels, how = st.page_labels(_scan(8, margins))
    assert labels[1:4] == ["ii", "iii", "iv"] and labels[4:] == ["1", "2", "3", "4"]


def test_pages_field_and_a_cover_sheet():
    labels, how = st.page_labels(_scan(7, [[]] * 7), pages_field="68-73")
    assert how == "pages field" and labels[0] == "" and labels[1] == "68" and labels[-1] == "73"
    labels, how = st.page_labels(_scan(3, [[]] * 3), pages_field="12-20")
    assert labels is None and how == "none"


def test_pdf_page_labels_win():
    labels, how = st.page_labels({"pages": 2, "labels": ["e1", "e2"], "margins": []})
    assert (labels, how) == (["e1", "e2"], "pdf-labels")


REFS = """Deci, E. L., & Ryan, R. M. (2000). The "what" and "why" of goal pursuits. Psychological Inquiry, 11(4), 227-268.
https://doi.org/10.1207/S15327965PLI1104_01
Haerens, L., Aelterman, N., Vansteenkiste, M., Soenens, B., & Van Petegem, S. (2015). Do perceived autonomy-supportive
and controlling teaching relate to physical education students' motivational experiences? Psychology of Sport and
Exercise, 16, 26-36. Ntoumanis, N. (2001). A self-determination approach to the understanding of motivation in
physical education. British Journal of Educational Psychology, 71(2), 225-242. Reeve, J. (2009). Why teachers adopt
a controlling motivating style toward students. Educational Psychologist, 44(3), 159-175."""

PROSE = """Autonomy-supportive teaching has been linked to students' motivation in many studies (Deci & Ryan, 2000;
Haerens et al., 2015). Teachers who acknowledge students' perspectives and offer meaningful choices foster
autonomous motivation, whereas controlling teaching undermines it (Reeve, 2009). In physical education, these
findings have been replicated across age groups and countries, although most studies relied on self-reports and
cross-sectional designs, which limits causal conclusions about the direction of these effects (Ntoumanis, 2001)."""


def test_reference_lists_are_recognised_by_their_shape():
    assert st.looks_like_references(REFS)
    assert not st.looks_like_references(PROSE)


def test_bookmarks_used_when_they_name_sections():
    toc = [[1, "Title of the paper", 1], [2, "Abstract", 1], [2, "Background", 1], [2, "Methods", 2],
           [3, "Participants", 2], [2, "Results", 4], [2, "References", 7]]
    hs = st.headings_from_toc(toc, 8, book_like=False)
    # The title bookmark holding everything is dropped; its children become the top level.
    assert [(h.level, h.section) for h in hs][:4] == [(1, "Abstract"), (1, "Introduction"), (1, "Methods"),
                                                      (2, "Methods")]
    assert st.headings_from_toc([[1, "Page 1", 1], [1, "Page 2", 2], [1, "Page 3", 3]], 3, book_like=False) == []


def _cand(i, p, text, feats=("bold",), alone=True):
    return {"id": f"c{i}", "p": p, "y": 100.0 + i, "text": text, "size": 11.0, "feats": list(feats),
            "numbered": False, "alone": alone, "gap": True, "centered": False}


def test_rules_drop_a_structured_abstract_and_author_roles():
    cands = [_cand(0, 0, "Background"), _cand(1, 0, "Methods"), _cand(2, 0, "Results"),
             _cand(3, 1, "Introduction"), _cand(4, 2, "Methods"), _cand(5, 4, "Results"),
             _cand(6, 6, "References"), _cand(7, 7, "Methodology")]
    hs = st.headings_from_rules(cands, book_like=False)
    assert [(h.page, h.section) for h in hs] == [(2, "Introduction"), (3, "Methods"), (5, "Results"),
                                                 (7, "References")]


def test_gemini_picks_only_candidates_in_reading_order():
    scan = {"pages": 10, "candidates": [_cand(0, 0, "A Title"), _cand(1, 1, "Introduction"),
                                        _cand(2, 3, "Method"), _cand(3, 5, "Figure 2 note")],
            "body": {"size": 10}, "contents": "", "running": []}
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        return json.dumps({"headings": [{"id": "c2", "level": 1, "section": "Methods"},
                                        {"id": "c1", "level": 1, "section": "Introduction"},
                                        {"id": "c99", "level": 1, "section": "Results"}]})

    hs, note = st.gemini_headings(scan, None, False, "A Title", ask)
    assert [h.text for h in hs] == ["Introduction", "Method"] and "c2 | 4 |" in prompts[0]
    assert st.gemini_headings(scan, None, False, "", lambda p: "not json")[0] == []


def _chunk(i, page, start, body, **meta):
    return {"id": f"K#{i}", "body": body,
            "meta": {"chunk_index": i, "page": page, "char_start": start, "char_end": start + len(body), **meta}}


def test_passages_get_section_heading_and_printed_page():
    chunks = [
        _chunk(0, 1, 0, "Title. Abstract Background: we asked why. Methods: children took part."),
        _chunk(1, 2, 80, "Introduction Children play. " + "x " * 40),
        _chunk(2, 3, 200, "Method Participants Forty children took part. " + "y " * 40),
        _chunk(3, 4, 340, "Results The children played more. " + "z " * 40),
        _chunk(4, 5, 480, REFS),
    ]
    structure = st.Structure(pages=5, labels=["377", "378", "379", "380", "381"], labels_from="printed numbers",
                             headings=[st.Heading(1, "Abstract", 1, "Abstract"),
                                       st.Heading(1, "Methods", 2, "Methods"),
                                       st.Heading(2, "Introduction", 1, "Introduction"),
                                       st.Heading(3, "Method", 1, "Methods"),
                                       st.Heading(3, "Participants", 2, "Methods"),
                                       st.Heading(4, "Results", 1, "Results")],
                             headings_from="bookmarks")
    metas, report = st.label_passages(chunks, structure, "journalArticle")
    assert [m.get("section") for m in metas] == ["Abstract", "Introduction", "Methods", "Results", "References"]
    assert metas[2]["heading"] == "Method › Participants" and metas[2]["page_label"] == "379"
    assert all(m["structure_v"] == st.STRUCTURE_VERSION for m in metas)
    assert report["located"] >= 5 and report["references"] == 1


def test_headings_not_found_in_the_indexed_text_are_not_used():
    chunks = [_chunk(i, i + 1, i * 100, "unrelated words " * 5) for i in range(5)]
    structure = st.Structure(pages=5, headings=[st.Heading(p, f"Heading number {p}", 1, None) for p in range(1, 6)],
                             headings_from="gemini")
    metas, report = st.label_passages(chunks, structure)
    assert report.get("mismatch") and not any("heading" in m for m in metas)


def test_scan_reads_style_bookmarks_labels_and_footer_numbers(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    body = "Children played on the playground while teachers watched and wrote notes about it. " * 6
    for i in range(4):
        page = doc.new_page()
        if i == 1:
            page.insert_text((72, 100), "2. Methods", fontsize=14, fontname="hebo")
        y = 130
        for k in range(0, len(body), 90):
            page.insert_text((72, y), body[k:k + 90], fontsize=10, fontname="helv")
            y += 14
        page.insert_text((290, 820), str(101 + i), fontsize=9, fontname="helv")
    doc.set_toc([[1, "Introduction", 1], [1, "Methods", 2], [1, "References", 4]])
    path = tmp_path / "t.pdf"
    doc.save(path)
    scan = st.scan_pdf(str(path))
    assert scan["pages"] == 4 and len(scan["toc"]) == 3
    assert any(c["text"] == "2. Methods" and "larger" in c["feats"] for c in scan["candidates"])
    labels, how = st.page_labels(scan)
    assert labels == ["101", "102", "103", "104"]
    result = st.analyse(scan, "journalArticle")
    assert result.headings_from == "bookmarks" and [h.section for h in result.headings][1] == "Methods"


# --- relabel: metadata-only updates of an existing index ------------------------------


class FakeCollection:
    def __init__(self, rows):
        self.rows = rows  # id -> (meta, doc)

    def get(self, ids=None, where=None, include=None):
        keys = ids if ids is not None else list(self.rows)
        if where:
            keys = [k for k in keys if self.rows[k][0].get("parent_item_key") == where["parent_item_key"]]
        keys = [k for k in keys if k in self.rows]
        return {"ids": keys, "metadatas": [self.rows[k][0] for k in keys], "documents": [self.rows[k][1] for k in keys]}


class FakeChroma:
    def __init__(self, rows):
        self.collection = FakeCollection(rows)
        self.embedding_config = {}
        self.updated = {}

    def update_metadatas(self, ids, metas):
        for i, m in zip(ids, metas):
            self.updated[i] = m
            self.collection.rows[i] = (m, self.collection.rows[i][1])


class FakeSearch:
    def __init__(self, rows):
        self.chroma_client = FakeChroma(rows)


class FakeReader:
    def __init__(self, path):
        self.path = path

    def get_attachment_paths(self, key):
        return [{"resolved_path": self.path, "exists": True}]

    def _get_connection(self):
        raise RuntimeError("no database in tests")


def test_relabel_updates_metadata_only_and_skips_done_items(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    from zotero_mcp import relabel

    monkeypatch.setattr(relabel, "structure_dir", lambda: tmp_path / "structure")
    doc = pymupdf.open()
    texts = ["Introduction Children play outside every day.", "Methods Forty children took part in the study.",
             "Results The children played more on the new playground."]
    for i, t in enumerate(texts):
        page = doc.new_page()
        page.insert_text((72, 100), t, fontsize=10)
        page.insert_text((290, 820), str(377 + i), fontsize=9)
    doc.set_toc([[1, "Introduction", 1], [1, "Methods", 2], [1, "Results", 3]])
    pdf = tmp_path / "a.pdf"
    doc.save(pdf)
    rows, start = {}, 0
    for i, t in enumerate(texts):
        rows[f"K#{i}"] = ({"parent_item_key": "K", "chunk_index": i, "page": i + 1, "char_start": start,
                          "char_end": start + len(t), "item_type": "journalArticle"}, t)
        start += len(t) + 1
    search = FakeSearch(rows)
    out = relabel.run(search=search, reader=FakeReader(pdf), gemini=False, log=lambda m: None, workers=1)
    assert out["totals"]["items"] == 1
    metas = [search.chroma_client.collection.rows[f"K#{i}"][0] for i in range(3)]
    assert [m.get("section") for m in metas] == ["Introduction", "Methods", "Results"]
    assert [m.get("page_label") for m in metas] == ["377", "378", "379"]
    # A second run finds nothing to do.
    out = relabel.run(search=search, reader=FakeReader(pdf), gemini=False, log=lambda m: None, workers=1)
    assert out["totals"].get("items", 0) == 0


def test_the_child_process_survives_a_windows_console_encoding(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Results", fontsize=12)
    doc.set_toc([[1, "Résultats ≥ 3: ﬁnal", 1]])
    path = tmp_path / "u.pdf"
    doc.save(path)
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    scan = st.read_pdf(path)
    assert scan is not None and scan["toc"][0][1] == "Résultats ≥ 3: ﬁnal"
    pages = st.read_first_pages(path, 1)
    assert pages is not None and pages["texts"]


def test_a_thread_bound_database_reader_is_used_from_one_thread_only(tmp_path):
    """While Zotero is open the reader's SQLite snapshot may only be used by the
    thread that opened it; SerialReader runs every call on that thread."""
    import sqlite3
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from zotero_mcp.local_db import SerialReader

    class ThreadBound:
        def __init__(self):
            self.conn = sqlite3.connect(":memory:", check_same_thread=True)
            self.owner = threading.get_ident()

        def lookup(self, x):
            self.conn.execute("select 1")   # raises from any other thread
            return x * 2

    serial = SerialReader(ThreadBound)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(serial.lookup, range(20))) == [x * 2 for x in range(20)]
    assert serial.run(lambda r: r.owner) != threading.get_ident()
    plain = ThreadBound()
    with ThreadPoolExecutor(max_workers=2) as pool, pytest.raises(sqlite3.ProgrammingError):
        list(pool.map(plain.lookup, range(4)))


def test_combined_sections_and_apa_introductions():
    assert st.second_section("Results and Discussion") == "Discussion"
    assert st.second_section("Methods") is None
    intro = "Children who play outside take risks, and theory says why. " * 30
    chunks = [_chunk(0, 1, 0, "Title.\nAbstract We asked why children play."),
              _chunk(1, 1, 60, "Abstract We asked why children take risks in play. " * 4),
              _chunk(2, 2, 3000, intro), _chunk(3, 3, 5000, "Method Participants Forty children. " * 10),
              _chunk(4, 4, 6000, "Results and Discussion The children played more. " * 10)]
    structure = st.Structure(pages=4, headings=[st.Heading(1, "Abstract", 1, "Abstract"),
                                                st.Heading(3, "Method", 1, "Methods"),
                                                st.Heading(4, "Results and Discussion", 1, "Results")],
                             headings_from="gemini")
    metas, _ = st.label_passages(chunks, structure, "journalArticle")
    assert [m.get("section") for m in metas] == ["Abstract", "Abstract", "Introduction", "Methods", "Results"]
    assert metas[4]["section_2"] == "Discussion"
    from zotero_mcp.semantic_search import _with_second_section
    assert _with_second_section({"section": "Discussion"}) == {
        "$or": [{"section": "Discussion"}, {"section_2": "Discussion"}]}
    assert _with_second_section({"item_type": "book"}) == {"item_type": "book"}


def test_introductions_with_and_without_a_heading():
    body = "Children who play outside take risks, and theory says why. " * 30
    method = "Method Participants Forty children. " * 10

    def sections(headings, chunks):
        structure = st.Structure(pages=6, headings=headings, headings_from="gemini")
        return [m.get("section") for m in st.label_passages(chunks, structure, "journalArticle")[0]]

    # APA with topical headings inside the introduction, and no Introduction heading.
    chunks = [_chunk(0, 1, 0, "Title.\nAbstract We asked why children play."),
              _chunk(1, 1, 60, "Abstract We asked why children take risks in play. " * 4),
              _chunk(2, 2, 3000, body), _chunk(3, 3, 5000, "Risky play and development " + body),
              _chunk(4, 4, 7000, "The Present Study " + body), _chunk(5, 5, 9000, method)]
    assert sections([st.Heading(1, "Abstract", 1, "Abstract"),
                     st.Heading(3, "Risky play and development", 1, None),
                     st.Heading(4, "The Present Study", 1, "Introduction"),
                     st.Heading(5, "Method", 1, "Methods")], chunks) == [
        "Abstract", "Abstract", "Introduction", "Introduction", "Introduction", "Methods"]

    # A paper that does have "1. Introduction": keywords before it stay unlabelled,
    # a topical section after it, before Method, is introduction too.
    chunks = [_chunk(0, 1, 0, "Title.\nAbstract We asked why children play."),
              _chunk(1, 1, 60, "Abstract We asked why children take risks in play. " * 4),
              _chunk(2, 1, 400, "Keywords: risky play; outdoor play; children"),
              _chunk(3, 2, 3000, "1. Introduction " + body),
              _chunk(4, 3, 5000, "2. Outdoor play in Belgium " + body), _chunk(5, 4, 7000, method)]
    assert sections([st.Heading(1, "Abstract", 1, "Abstract"),
                     st.Heading(2, "1. Introduction", 1, "Introduction"),
                     st.Heading(3, "2. Outdoor play in Belgium", 1, None),
                     st.Heading(4, "3. Method", 1, "Methods")], chunks) == [
        "Abstract", "Abstract", "Abstract", "Introduction", "Introduction", "Methods"]


def test_level_two_introduction_headings_are_not_parts_of_the_abstract():
    body = "Children who play outside take risks, and theory says why. " * 30
    chunks = [_chunk(0, 1, 0, "Title.\nAbstract We asked why."),
              _chunk(1, 1, 60, "Abstract We asked why children take risks. " * 4),
              _chunk(2, 2, 3000, body), _chunk(3, 3, 5000, "Risky Play and Development " + body),
              _chunk(4, 4, 7000, "The Present Study " + body),
              _chunk(5, 5, 9000, "Method Participants Forty children. " * 10)]
    # APA 7: the introduction's sub-headings are level 2; the repeated title was not found.
    heads = [st.Heading(1, "Abstract", 1, "Abstract"), st.Heading(3, "Risky Play and Development", 2, None),
             st.Heading(4, "The Present Study", 2, "Introduction"), st.Heading(5, "Method", 1, "Methods")]
    metas, _ = st.label_passages(chunks, st.Structure(pages=5, headings=heads, headings_from="gemini"),
                                 "journalArticle")
    assert [(m.get("section"), m.get("heading")) for m in metas[1:]] == [
        ("Abstract", "Abstract"), ("Introduction", None), ("Introduction", "Risky Play and Development"),
        ("Introduction", "The Present Study"), ("Methods", "Method")]


def test_gemini_asker_sends_no_temperature_and_low_thinking(monkeypatch):
    from types import SimpleNamespace

    from zotero_mcp import gemini_batch, gemini_util

    sent = []

    class Models:
        def generate_content(self, model, contents, config):
            sent.append(config)
            if config.thinking_config is not None and len(sent) == 1 and model == "gemini-2.5-flash":
                raise ValueError("thinking_level is not supported for this model")
            return SimpleNamespace(text='{"ok": true}')

    monkeypatch.setattr(gemini_batch, "create_gemini_client", lambda cfg: SimpleNamespace(models=Models()))
    ask = gemini_util.json_asker(gemini_util.DEFAULT_MODEL, {"type": "object"})
    assert ask("x") == '{"ok": true}'
    assert sent[0].temperature is None and str(sent[0].thinking_config.thinking_level).endswith("LOW")
    assert gemini_util.DEFAULT_MODEL == "gemini-3.8-flash"
    sent.clear()
    old = gemini_util.json_asker("gemini-2.5-flash", {"type": "object"})
    assert old("x") == '{"ok": true}' and old("y") == '{"ok": true}'
    assert [c.thinking_config is None for c in sent] == [False, True, True]


def test_bookmarks_cleaned_and_incomplete_ones_refused():
    toc = [[1, "\ufeffAbstract", 1], [1, "Introduction", 2], [1, "Method", 4], [1, "Results", 8],
           [1, "References", 12], [1, "Table 1:", 14], [1, "Figures", 15]]
    hs = st.headings_from_toc(toc, 16, book_like=False)
    assert [h.text for h in hs] == ["Abstract", "Introduction", "Method", "Results", "References"]
    # They stop at the method of a 25-page paper: not used.
    half = [[1, "Title", 1], [2, "Abstract", 1], [2, "The present study", 4], [2, "Method", 4],
            [3, "Participants", 4], [3, "Measurements", 6]]
    assert st.headings_from_toc(half, 25, book_like=False) == []


def test_section_names_for_abstract_lines_appendices_and_studies():
    assert st.canonical_section("Abstract: Teachers' professional development is crucial for practice") == "Abstract"
    assert st.canonical_section("ABSTRACT—Despite a long tradition of effectiveness in") == "Abstract"
    assert st.canonical_section("Appendix 2: Factor analyses on the questionnaires") == "Appendix"
    assert st.is_study_heading("STUDY 1") and st.is_study_heading("Experiment 3 and 4")
    assert not st.is_study_heading("Study 1: Method") and not st.is_study_heading("Study design")


def test_headings_are_found_in_order_and_on_their_page():
    body1 = "Abstract\nBackground: children play. Methods: forty children. Results: they played more."
    chunks = [_chunk(0, 1, 0, body1),
              _chunk(1, 2, 1000, "Introduction\nChildren play outside. " * 20),
              _chunk(2, 3, 2000, "Methods\nForty children took part in Phase 1. " * 10),
              _chunk(3, 3, 2500, "Phase 1\nChildren were observed. " * 10),
              _chunk(4, 4, 3000, "Results\nIn Phase 1 the children played more. " * 10),
              _chunk(5, 4, 3500, "Phase 1\nMore play than expected. " * 10)]
    heads = [st.Heading(1, "Abstract", 1, "Abstract"), st.Heading(2, "Introduction", 1, "Introduction"),
             st.Heading(3, "Methods", 1, "Methods"), st.Heading(3, "Phase 1", 2, None),
             st.Heading(4, "Results", 1, "Results"), st.Heading(4, "Phase 1", 2, None)]
    metas, rep = st.label_passages(chunks, st.Structure(pages=4, headings=heads, headings_from="bookmarks"),
                                   "journalArticle")
    assert [m.get("section") for m in metas] == ["Abstract", "Introduction", "Methods", "Methods", "Results",
                                                 "Results"]
    assert metas[5]["heading"] == "Results › Phase 1" and metas[3]["heading"] == "Methods › Phase 1"


def test_pdf_labels_that_are_not_page_numbers_are_not_used():
    bad = st._implausible_labels
    assert bad(["image 1", "image 2", "image 3"], False)
    assert bad(["1", "0", "1", "2", "3"], False)
    assert bad(["i", "ii", "iii", "iv", "v", "vi"], False)
    assert not bad(["", "555", "556", "557"], False)
    assert not bad(["i", "ii", "1", "2", "3"], False)
    assert not bad(["Cover", "i", "ii", "1", "2", "3", "4", "5", "1", "2"], True)
