"""Tests for the EPUB exporter."""

import base64
import io
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from novelhog.exporter.base import BookChapter, BookInput
from novelhog.exporter.epub import EpubExporter

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "o": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "x": "http://www.w3.org/1999/xhtml",
}


def _img(fmt: str, mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    colour = (255, 0, 0, 0) if mode == "RGBA" else "red"
    Image.new(mode, (4, 4), colour).save(out, fmt)
    return out.getvalue()


def _data_md(data: bytes, mime: str) -> str:
    return f"![alt](data:{mime};base64,{base64.b64encode(data).decode()})"


def _book(chapters: list[BookChapter], **kw: object) -> BookInput:
    fields: dict[str, object] = {
        "identifier": "urn:novelhog:EX1",
        "title": "My Book",
        "language": "en",
        "cover": None,
    }
    fields.update(kw)
    return BookInput(chapters=chapters, **fields)  # type: ignore[arg-type]


def _build(tmp_path: Path, book: BookInput) -> zipfile.ZipFile:
    out = tmp_path / "b.epub"
    EpubExporter().build(book, out)
    return zipfile.ZipFile(out)


def _opf(z: zipfile.ZipFile) -> ET.Element:
    container = ET.fromstring(z.read("META-INF/container.xml"))
    rootfile = container.find(".//c:rootfile", NS)
    assert rootfile is not None
    path = rootfile.get("full-path")
    assert path is not None
    return ET.fromstring(z.read(path))


def _spine(z: zipfile.ZipFile) -> list[str]:
    """The spine's documents by href, in reading order."""
    opf = _opf(z)
    hrefs = {i.get("id"): i.get("href") for i in opf.findall(".//o:manifest/o:item", NS)}
    return [str(hrefs[r.get("idref")]) for r in opf.findall(".//o:spine/o:itemref", NS)]


def _chapter_docs(z: zipfile.ZipFile) -> list[str]:
    return sorted(n for n in z.namelist() if "chapter_" in n)


CH = [BookChapter(2, "Second", "two"), BookChapter(1, "First", "one")]


class TestEpubExporter:
    def test_build__produces_valid_epub3_zip(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH))
        first = z.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        opf = _opf(z)
        assert opf.get("version") == "3.0"
        assert any(n.endswith("nav.xhtml") for n in z.namelist())

    def test_build__spine_and_nav_follow_position_order(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book([BookChapter(3, "Third", "c"), *CH]))
        opf = _opf(z)
        hrefs = {i.get("id"): i.get("href") for i in opf.findall(".//o:manifest/o:item", NS)}
        spine = [hrefs[r.get("idref")] for r in opf.findall(".//o:spine/o:itemref", NS)]
        chapters = [h for h in spine if h and h.startswith("chapter_")]
        assert chapters == ["chapter_00001.xhtml", "chapter_00002.xhtml", "chapter_00003.xhtml"]
        nav = ET.fromstring(z.read("EPUB/nav.xhtml"))
        assert [a.text for a in nav.findall(".//x:nav//x:a", NS)] == ["First", "Second", "Third"]

    def test_build__one_document_per_chapter_with_title_heading(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH))
        docs = _chapter_docs(z)
        assert len(docs) == 2
        heads = [ET.fromstring(z.read(d)).find(".//x:h1", NS).text for d in docs]  # type: ignore[union-attr]
        assert heads == ["First", "Second"]

    def test_build__empty_title_falls_back_to_chapter_position(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book([BookChapter(7, "\x00", "x")]))
        doc = ET.fromstring(z.read(_chapter_docs(z)[0]))
        assert doc.find(".//x:h1", NS).text == "Chapter 7"  # type: ignore[union-attr]

    def test_build__mislabelled_webp_leaves_no_webp(self, tmp_path: Path) -> None:
        webp = _img("WEBP")
        md = _data_md(webp, "image/png")
        z = _build(tmp_path, _book([BookChapter(1, "A", md)], cover=(webp, "image/png")))
        for name in z.namelist():
            assert not name.endswith(".webp")
            if "images/" in name or "cover." in name:
                assert not z.read(name).startswith(b"RIFF")

    def test_build__truncated_image_dropped_and_book_builds(self, tmp_path: Path) -> None:
        md = "x " + _data_md(b"GIF89a", "image/gif")
        z = _build(tmp_path, _book([BookChapter(1, "A", md)], cover=(b"GIF89a", "image/gif")))
        assert not [n for n in z.namelist() if "img_" in n or "cover." in n]
        assert "<img" not in z.read(_chapter_docs(z)[0]).decode()

    def test_build__invalid_base64_image_becomes_alt_text(self, tmp_path: Path) -> None:
        md = "![the alt](data:image/png;base64,!!!)"
        z = _build(tmp_path, _book([BookChapter(1, "A", md)]))
        assert "the alt" in z.read(_chapter_docs(z)[0]).decode()

    def test_build__sets_cover_converted(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH, cover=(_img("WEBP"), "image/webp")))
        covers = [n for n in z.namelist() if "cover" in n and n.endswith((".jpg", ".png"))]
        assert covers
        assert not any(n.endswith(".webp") for n in z.namelist())

    def test_build__cover_page_opens_the_book(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH, cover=(_img("JPEG"), "image/jpeg")))
        spine = _spine(z)
        assert spine[:2] == ["cover.xhtml", "nav.xhtml"]
        assert all(r.get("linear") != "no" for r in _opf(z).findall(".//o:spine/o:itemref", NS))
        page = ET.fromstring(z.read("EPUB/cover.xhtml"))
        assert page.find(".//x:img", NS).get("src") == "cover.jpg"  # type: ignore[union-attr]

    def test_build__cover_page_is_the_guides_and_landmarks_cover(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH, cover=(_img("JPEG"), "image/jpeg")))
        references = _opf(z).findall(".//o:guide/o:reference", NS)
        assert [(r.get("type"), r.get("href")) for r in references] == [("cover", "cover.xhtml")]
        nav = ET.fromstring(z.read("EPUB/nav.xhtml"))
        landmarks = [n for n in nav.findall(".//x:nav", NS) if "landmarks" in str(n.attrib)]
        assert [a.get("href") for a in landmarks[0].findall(".//x:a", NS)] == ["cover.xhtml"]

    def test_build__without_a_cover_the_book_opens_at_the_nav(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH))
        assert _spine(z)[0] == "nav.xhtml"
        assert _opf(z).find(".//o:guide", NS) is None

    def test_build__unusable_cover_adds_no_cover_page(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH, cover=(b"GIF89a", "image/gif")))
        assert _spine(z)[0] == "nav.xhtml"
        assert "EPUB/cover.xhtml" not in z.namelist()

    def test_build__inline_images_become_items(self, tmp_path: Path) -> None:
        md = _data_md(_img("PNG"), "image/png")
        z = _build(tmp_path, _book([BookChapter(1, "A", md)]))
        names = [n for n in z.namelist() if "img_" in n]
        assert len(names) == 1
        doc = ET.fromstring(z.read(_chapter_docs(z)[0]))
        assert doc.find(".//x:img", NS).get("src") == "images/img_00001.png"  # type: ignore[union-attr]
        assert "data:" not in z.read(_chapter_docs(z)[0]).decode()

    def test_build__webp_converted_no_webp_in_book(self, tmp_path: Path) -> None:
        md = (
            _data_md(_img("WEBP"), "image/webp")
            + "\n\n"
            + _data_md(_img("WEBP", "RGBA"), "image/webp")
        )
        z = _build(tmp_path, _book([BookChapter(1, "A", md)]))
        assert not any("webp" in n for n in z.namelist())
        opf = z.read([n for n in z.namelist() if n.endswith(".opf")][0]).decode()
        assert "webp" not in opf
        assert "image/png" in opf
        assert "image/jpeg" in opf

    def test_build__raw_html_is_literal(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book([BookChapter(1, "A", "<script>alert(1)</script> hi")]))
        raw = z.read(_chapter_docs(z)[0]).decode()
        assert "<script>" not in raw
        assert "&lt;script&gt;" in raw

    def test_build__sets_metadata(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book(CH, language="ru"))
        meta = _opf(z).find("o:metadata", NS)
        assert meta is not None
        assert meta.find("dc:title", NS).text == "My Book"  # type: ignore[union-attr]
        assert meta.find("dc:identifier", NS).text == "urn:novelhog:EX1"  # type: ignore[union-attr]
        assert meta.find("dc:language", NS).text == "ru"  # type: ignore[union-attr]
        modified = [
            m for m in meta.findall("o:meta", NS) if m.get("property") == "dcterms:modified"
        ]
        assert len(modified) == 1
        assert modified[0].text

    def test_build__every_xml_document_is_well_formed(self, tmp_path: Path) -> None:
        md = '# h\n\n& < > "q" `code` [l](http://x)\n\n' + _data_md(_img("GIF"), "image/gif")
        z = _build(tmp_path, _book(CH + [BookChapter(3, "A & B <c>", md)]))
        for name in z.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(z.read(name))


class TestEpubExporter__WithXmlInvalidCharacters:
    def test_build__removes_them_and_builds(self, tmp_path: Path) -> None:
        z = _build(tmp_path, _book([BookChapter(1, "Ti\x00tle\x0b", "bo\x00dy")], title="Bo\x01ok"))
        meta = _opf(z).find("o:metadata", NS)
        assert meta.find("dc:title", NS).text == "Book"  # type: ignore[union-attr]
        doc = ET.fromstring(z.read(_chapter_docs(z)[0]))
        assert doc.find(".//x:h1", NS).text == "Title"  # type: ignore[union-attr]


class TestEpubExporter__WithNoChapters:
    def test_build__raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="chapters"):
            EpubExporter().build(_book([]), tmp_path / "x.epub")
