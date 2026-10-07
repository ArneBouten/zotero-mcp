"""Citation markers in .docx files become live Zotero citation fields.

The fixtures are built by hand as minimal WordprocessingML packages, so the
tests pin the exact XML Zotero's Word plugin reads: a complex field whose
instruction is ``ADDIN ZOTERO_ITEM CSL_CITATION {json}``, whose result text
equals the JSON's ``plainCitation``, and document preferences in the custom
properties ``ZOTERO_PREF_n``.
"""

import json
import re
import zipfile

import pytest

etree = pytest.importorskip("lxml.etree")

from zotero_mcp import word_citations as wc  # noqa: E402

W_NS = wc.W_NS
NSMAP_DECL = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'mc:Ignorable="w14"'
)
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/></Relationships>'
)


def run(text, bold=False, rsid=None):
    attrs = f' w:rsidR="{rsid}"' if rsid else ""
    rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r{attrs}>{rpr}<w:t xml:space="preserve">{text}</w:t></w:r>'


def para(*runs, ppr=""):
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def make_docx(path, *paragraphs, custom_xml=None, extra_files=None):
    body = "".join(paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {NSMAP_DECL}><w:body>{body}<w:sectPr/></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", document)
        if custom_xml:
            z.writestr("docProps/custom.xml", custom_xml)
        for name, data in (extra_files or {}).items():
            z.writestr(name, data)
    return path


def item(key, citation, year="2009", bib=None):
    return wc.ResolvedItem(
        key=key,
        uri=f"http://zotero.org/users/7756991/items/{key}",
        item_data={"id": key, "type": "article-journal", "title": f"Title {key}",
                   "issued": {"date-parts": [[year]]}},
        citation=citation,
        bibliography=bib or f"{citation.strip('()')}. Title {key}.",
        item_id=hash(key) % 1000,
    )


LIBRARY = {
    "SOEN2009": item("SOEN2009", "(Soenens et al., 2009)"),
    "GROL2009": item("GROL2009", "(Grolnick & Pomerantz, 2009)"),
    "grolnick2009issues": item("GROL2009", "(Grolnick & Pomerantz, 2009)"),
    "BION1962": item("BION1962", "(Bion, 1962)", year="1962"),
}


def resolver(calls=None):
    def resolve(keys):
        if calls is not None:
            calls.append(list(keys))
        return {k: LIBRARY[k] for k in keys if k in LIBRARY}

    return resolve


def read(path, name="word/document.xml"):
    with zipfile.ZipFile(path) as z:
        return z.read(name).decode("utf-8")


def fields(xml):
    """(code, result_text) for every complex field, joined across runs."""
    root = etree.fromstring(xml.encode())
    out = []
    state, code, result = None, [], []
    for el in root.iter():
        if el.tag == f"{{{W_NS}}}fldChar":
            kind = el.get(f"{{{W_NS}}}fldCharType")
            if kind == "begin":
                state, code, result = "code", [], []
            elif kind == "separate":
                state = "result"
            elif kind == "end":
                out.append(("".join(code), "".join(result)))
                state = None
        elif el.tag == f"{{{W_NS}}}instrText" and state == "code":
            code.append(el.text or "")
        elif el.tag == f"{{{W_NS}}}t" and state == "result":
            result.append(el.text or "")
    return out


def visible_text(xml):
    root = etree.fromstring(xml.encode())
    return "".join(t.text or "" for t in root.iter(f"{{{W_NS}}}t"))


def citation_json(code):
    assert code.startswith(" ADDIN ZOTERO_ITEM CSL_CITATION ")
    return json.loads(code[len(" ADDIN ZOTERO_ITEM CSL_CITATION "):].strip())


# --- marker parsing -----------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("[@ABCD1234]", [("ABCD1234", "", "", "", "", False)]),
        ("[@a; @b]", [("a", "", "", "", "", False), ("b", "", "", "", "", False)]),
        ("[@k, p. 12]", [("k", "", "page", "12", "", False)]),
        ("[@k, pp. 33-35, emphasis added]", [("k", "", "page", "33-35", "emphasis added", False)]),
        ("[@k, 12]", [("k", "", "page", "12", "", False)]),
        ("[@k, chap. 3]", [("k", "", "chapter", "3", "", False)]),
        ("[@k, p. iv]", [("k", "", "page", "iv", "", False)]),
        ("[@k, italics added]", [("k", "", "", "", "italics added", False)]),
        ("[see @k, p. 3; also @j]", [("k", "see", "page", "3", "", False), ("j", "also", "", "", "", False)]),
        ("[-@k]", [("k", "", "", "", "", True)]),
        ("[@kasser1993dark]", [("kasser1993dark", "", "", "", "", False)]),
    ],
)
def test_parse_markers(text, expected):
    clusters = wc.find_clusters(text)
    assert len(clusters) == 1
    got = [(i.key, i.prefix, i.label, i.locator, i.suffix, i.suppress_author) for i in clusters[0].items]
    assert got == expected


@pytest.mark.parametrize("text", ["[1]", "[me@example.org]", "an @handle", "[see table 2]", "[@]"])
def test_non_markers(text):
    assert wc.find_clusters(text) == []


def test_marker_offsets():
    text = "A [@a] and B [@b, p. 2]."
    clusters = wc.find_clusters(text)
    assert [text[c.start:c.end] for c in clusters] == ["[@a]", "[@b, p. 2]"]


# --- provisional text ---------------------------------------------------------------


def _cluster(text):
    return wc.find_clusters(text)[0]


def test_provisional_text_variants():
    lib = LIBRARY
    assert wc.provisional_text(_cluster("[@SOEN2009]"), lib) == "(Soenens et al., 2009)"
    assert (
        wc.provisional_text(_cluster("[@SOEN2009; @GROL2009]"), lib)
        == "(Soenens et al., 2009; Grolnick & Pomerantz, 2009)"
    )
    assert wc.provisional_text(_cluster("[@SOEN2009, p. 12]"), lib) == "(Soenens et al., 2009, p. 12)"
    assert wc.provisional_text(_cluster("[@SOEN2009, pp. 12-14]"), lib) == "(Soenens et al., 2009, pp. 12-14)"
    assert wc.provisional_text(_cluster("[see @BION1962]"), lib) == "(see Bion, 1962)"
    assert wc.provisional_text(_cluster("[-@BION1962]"), lib) == "(1962)"


# --- conversion -----------------------------------------------------------------------


def test_single_run_marker_becomes_a_field(tmp_path):
    src = make_docx(tmp_path / "paper.docx", para(run("Control matters [@SOEN2009, p. 12] a lot.")))
    report = wc.convert_docx(src, resolver())
    out = tmp_path / "paper (Zotero).docx"
    assert report.output_path == str(out)
    assert report.citations == 1 and report.items == 1
    xml = read(out)
    [(code, result)] = fields(xml)
    data = citation_json(code)
    assert result == "(Soenens et al., 2009, p. 12)"
    assert data["properties"]["plainCitation"] == result
    assert data["properties"]["formattedCitation"] == result
    assert data["properties"]["noteIndex"] == 0
    [ci] = data["citationItems"]
    assert ci["uris"] == ["http://zotero.org/users/7756991/items/SOEN2009"]
    assert ci["locator"] == "12" and ci["label"] == "page"
    assert ci["itemData"]["title"] == "Title SOEN2009"
    assert ci["id"] == ci["itemData"]["id"] == LIBRARY["SOEN2009"].item_id
    assert data["schema"] == wc.CSL_SCHEMA
    assert re.fullmatch(r"[A-Za-z0-9]{8}", data["citationID"])
    assert visible_text(xml) == "Control matters (Soenens et al., 2009, p. 12) a lot."
    # The input is untouched.
    assert "[@SOEN2009, p. 12]" in read(src)


def test_marker_split_across_runs_keeps_surrounding_formatting(tmp_path):
    src = make_docx(
        tmp_path / "p.docx",
        para(run("Before [@SO", rsid="00A1"), run("EN2009; @GR", bold=True), run("OL2009] after", rsid="00B2")),
    )
    report = wc.convert_docx(src, resolver())
    assert report.citations == 1
    xml = read(tmp_path / "p (Zotero).docx")
    assert visible_text(xml) == "Before (Soenens et al., 2009; Grolnick & Pomerantz, 2009) after"
    [(code, result)] = fields(xml)
    assert [c["uris"][0][-8:] for c in citation_json(code)["citationItems"]] == ["SOEN2009", "GROL2009"]
    root = etree.fromstring(xml.encode())
    texts = [(t.text, t.getparent().get(f"{{{W_NS}}}rsidR")) for t in root.iter(f"{{{W_NS}}}t")]
    assert texts[0] == ("Before ", "00A1")
    assert texts[-1] == (" after", "00B2")


def test_several_markers_in_one_paragraph_and_citekeys(tmp_path):
    src = make_docx(
        tmp_path / "p.docx",
        para(run("A [@SOEN2009] B [@grolnick2009issues, p. 3] C [-@BION1962] D")),
    )
    report = wc.convert_docx(src, resolver())
    assert report.citations == 3
    xml = read(tmp_path / "p (Zotero).docx")
    assert visible_text(xml) == (
        "A (Soenens et al., 2009) B (Grolnick & Pomerantz, 2009, p. 3) C (1962) D"
    )
    codes = [citation_json(c) for c, _ in fields(xml)]
    assert codes[1]["citationItems"][0]["uris"][0].endswith("/GROL2009")
    assert codes[2]["citationItems"][0]["suppress-author"] is True
    # Field markup is balanced.
    assert xml.count('fldCharType="begin"') == xml.count('fldCharType="end"') == 3


def test_text_after_a_marker_that_ends_its_run_survives(tmp_path):
    # Regression: a marker ending exactly at a run boundary took every later
    # run of the paragraph with it.
    src = make_docx(tmp_path / "p.docx", para(run("A [@SOEN2009]"), run(" and B.", bold=True), run(" C.")))
    wc.convert_docx(src, resolver())
    assert visible_text(read(tmp_path / "p (Zotero).docx")) == "A (Soenens et al., 2009) and B. C."


def test_marker_at_paragraph_start_and_end(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009]")), para(run("x [@BION1962]")))
    wc.convert_docx(src, resolver())
    xml = read(tmp_path / "p (Zotero).docx")
    assert visible_text(xml) == "(Soenens et al., 2009)x (Bion, 1962)"
    assert len(fields(xml)) == 2


def test_unresolved_keys_are_reported_and_left_alone(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("Known [@SOEN2009], unknown [@NOPE0000; @SOEN2009].")))
    report = wc.convert_docx(src, resolver())
    assert report.unresolved == ["NOPE0000"]
    assert report.skipped == ["[@NOPE0000; @SOEN2009]"]
    assert report.citations == 1
    xml = read(tmp_path / "p (Zotero).docx")
    assert "[@NOPE0000; @SOEN2009]" in visible_text(xml)


def test_resolver_is_called_once_with_every_key(tmp_path):
    calls = []
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009] [@BION1962]")), para(run("[@SOEN2009]")))
    wc.convert_docx(src, resolver(calls))
    assert calls == [["BION1962", "SOEN2009"]]


def test_marker_straddling_a_hyperlink_is_skipped(tmp_path):
    src = make_docx(
        tmp_path / "p.docx",
        para(run("see [@SO"), '<w:hyperlink r:id="rId9">' + run("EN2009]") + "</w:hyperlink>"),
    )
    report = wc.convert_docx(src, resolver())
    assert report.citations == 0 and report.skipped == ["[@SOEN2009]"]


def test_bibliography_paragraph(tmp_path):
    src = make_docx(
        tmp_path / "p.docx",
        para(run("Text [@SOEN2009; @BION1962].")),
        para(run("{{bibliography}}"), ppr='<w:pPr><w:pStyle w:val="Normal"/></w:pPr>'),
        para(run("After the references.")),
    )
    report = wc.convert_docx(src, resolver())
    assert report.bibliography
    xml = read(tmp_path / "p (Zotero).docx")
    codes = fields(xml)
    assert len(codes) == 2
    code, result = codes[1]
    assert code == wc.BIBL_CODE
    assert "Bion, 1962. Title BION1962." in result and "Soenens et al., 2009. Title SOEN2009." in result
    root = etree.fromstring(xml.encode())
    paras = [visible_text(etree.tostring(p).decode()) for p in root.iter(f"{{{W_NS}}}p")]
    # One paragraph per entry, alphabetical; the following text is untouched.
    assert paras[1].startswith("Bion") and paras[2].startswith("Soenens")
    assert paras[3] == "After the references."
    assert "{{bibliography}}" not in visible_text(xml)


def test_document_preferences_are_added(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009]")))
    report = wc.convert_docx(src, resolver())
    assert report.prefs_written
    out = tmp_path / "p (Zotero).docx"
    prefs = wc.read_document_prefs(read(out, "docProps/custom.xml").encode())
    assert '<style id="http://www.zotero.org/styles/apa" locale="en-US"' in prefs
    assert '<pref name="fieldType" value="Field"/>' in prefs
    assert '<data data-version="3"' in prefs
    assert "/docProps/custom.xml" in read(out, "[Content_Types].xml")
    assert "relationships/custom-properties" in read(out, "_rels/.rels")


def test_long_preferences_are_split_into_255_character_pieces():
    prefs = "x" * 600
    xml = wc.write_document_prefs(None, prefs)
    root = etree.fromstring(xml)
    values = [p[0].text for p in root]
    assert [len(v) for v in values] == [255, 255, 90]
    assert [p.get("name") for p in root] == ["ZOTERO_PREF_1", "ZOTERO_PREF_2", "ZOTERO_PREF_3"]
    assert wc.read_document_prefs(xml) == prefs


EXISTING_CUSTOM = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
    'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="ZOTERO_PREF_1"><vt:lpwstr>'
    '&lt;data data-version="3" zotero-version="7.0.16"&gt;&lt;session id="PljAAdcp"/&gt;'
    '&lt;style id="http://www.zotero.org/styles/chicago-author-date" locale="nl-NL" hasBibliography="1" '
    'bibliographyStyleHasBeenSet="1"/&gt;&lt;prefs&gt;&lt;pref name="fieldType" value="Field"/&gt;'
    '&lt;/prefs&gt;&lt;/data&gt;</vt:lpwstr></property>'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="3" name="GrammarlyDocumentId">'
    "<vt:lpwstr>abc</vt:lpwstr></property></Properties>"
)


def test_existing_zotero_document_keeps_its_preferences(tmp_path):
    src = make_docx(
        tmp_path / "p.docx",
        para(run("old "), '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
             '<w:r><w:instrText xml:space="preserve"> ADDIN ZOTERO_ITEM CSL_CITATION {} </w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + run("(Old, 2000)")
             + '<w:r><w:fldChar w:fldCharType="end"/></w:r>', run(" new [@SOEN2009]")),
        custom_xml=EXISTING_CUSTOM,
    )
    report = wc.convert_docx(src, resolver())
    assert report.style == "chicago-author-date"
    assert report.existing_citations == 1
    assert not report.prefs_written
    out = tmp_path / "p (Zotero).docx"
    assert read(out, "docProps/custom.xml") == EXISTING_CUSTOM
    assert len(fields(read(out))) == 2


def test_namespaces_and_other_parts_survive(tmp_path):
    styles = f'<?xml version="1.0"?><w:styles xmlns:w="{W_NS}"/>'
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009]")), extra_files={"word/styles.xml": styles})
    wc.convert_docx(src, resolver())
    out = tmp_path / "p (Zotero).docx"
    xml = read(out)
    root = etree.fromstring(xml.encode())
    # mc:Ignorable names w14 by prefix; both must still be declared as such.
    assert root.nsmap["w14"] == "http://schemas.microsoft.com/office/word/2010/wordml"
    assert root.get("{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable") == "w14"
    assert xml.startswith("<?xml")
    assert read(out, "word/styles.xml") == styles
    with zipfile.ZipFile(src) as a, zipfile.ZipFile(out) as b:
        assert [i.filename for i in a.infolist()] == [i.filename for i in b.infolist()][: len(a.infolist())]


def test_dry_run_writes_nothing(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009] {{bibliography}}")), para(run("{{bibliography}}")))
    report = wc.convert_docx(src, resolver(), dry_run=True)
    assert report.dry_run and report.citations == 1 and report.bibliography
    assert not (tmp_path / "p (Zotero).docx").exists()


def test_in_place_keeps_a_backup(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009]")))
    original = src.read_bytes()
    report = wc.convert_docx(src, resolver(), in_place=True)
    assert report.output_path == str(src)
    assert (tmp_path / "p.bak.docx").read_bytes() == original
    assert len(fields(read(src))) == 1


def test_refuses_a_document_open_in_word(tmp_path):
    src = make_docx(tmp_path / "paper.docx", para(run("[@SOEN2009]")))
    (tmp_path / "~$paper.docx").write_bytes(b"owner")
    with pytest.raises(wc.WordCitationError, match="open in Word"):
        wc.convert_docx(src, resolver(), in_place=True)
    long = make_docx(tmp_path / "manuscript.docx", para(run("[@SOEN2009]")))
    (tmp_path / "~$nuscript.docx").write_bytes(b"owner")
    with pytest.raises(wc.WordCitationError, match="open in Word"):
        wc.convert_docx(long, resolver(), in_place=True)
    # Writing elsewhere is fine while the source is open.
    assert wc.convert_docx(long, resolver()).citations == 1


def test_custom_output_path_and_bad_inputs(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("[@SOEN2009]")))
    target = tmp_path / "out" / "final.docx"
    target.parent.mkdir()
    assert wc.convert_docx(src, resolver(), output_path=target).output_path == str(target)
    assert target.exists()
    with pytest.raises(wc.WordCitationError, match="No such file"):
        wc.convert_docx(tmp_path / "missing.docx", resolver())
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(wc.WordCitationError, match="Only .docx"):
        wc.convert_docx(tmp_path / "notes.txt", resolver())
    bad = tmp_path / "bad.docx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(wc.WordCitationError, match="not a valid .docx"):
        wc.convert_docx(bad, resolver())


def test_footnotes_are_converted(tmp_path):
    footnotes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:footnotes {NSMAP_DECL}><w:footnote w:id=\"1\">{para(run('See [@BION1962].'))}</w:footnote></w:footnotes>"
    )
    src = make_docx(tmp_path / "p.docx", para(run("Body.")), extra_files={"word/footnotes.xml": footnotes})
    report = wc.convert_docx(src, resolver())
    assert report.citations == 1
    assert len(fields(read(tmp_path / "p (Zotero).docx", "word/footnotes.xml"))) == 1


def test_nothing_to_do_leaves_content_unchanged(tmp_path):
    src = make_docx(tmp_path / "p.docx", para(run("No markers here.")))
    report = wc.convert_docx(src, resolver())
    assert report.citations == 0 and not report.prefs_written
    assert read(tmp_path / "p (Zotero).docx") == read(src)


def test_python_docx_document_round_trips(tmp_path):
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.add_paragraph("Psychological control predicts maladjustment [@SOEN2009].")
    d.add_paragraph("{{bibliography}}")
    path = tmp_path / "pd.docx"
    d.save(path)
    report = wc.convert_docx(path, resolver())
    assert report.citations == 1 and report.bibliography
    reopened = docx.Document(tmp_path / "pd (Zotero).docx")
    assert reopened.paragraphs[0].text.endswith("(Soenens et al., 2009).")


# --- Zotero item -> CSL fallback -------------------------------------------------------


def test_zotero_data_to_csl():
    csl = wc.zotero_data_to_csl(
        {
            "key": "ABCD1234", "itemType": "bookSection", "title": "Chapter", "bookTitle": "Book",
            "date": "2018-03-04", "pages": "1-10", "publisher": "Routledge",
            "creators": [
                {"creatorType": "author", "firstName": "M.", "lastName": "Davis"},
                {"creatorType": "editor", "name": "Editors Collective"},
            ],
        },
        item_id=12,
    )
    assert csl["id"] == 12 and csl["type"] == "chapter"
    assert csl["container-title"] == "Book" and csl["page"] == "1-10"
    assert csl["author"] == [{"family": "Davis", "given": "M."}]
    assert csl["editor"] == [{"literal": "Editors Collective"}]
    assert csl["issued"] == {"date-parts": [["2018", "03", "04"]]}
