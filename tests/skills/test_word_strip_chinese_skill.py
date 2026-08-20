"""Tests for optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "productivity"
    / "word-strip-chinese"
    / "scripts"
    / "strip_chinese.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("word_strip_chinese", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _paragraph(text: str) -> str:
    return (
        "<w:p><w:r>"
        f'<w:t xml:space="preserve">{text}</w:t>'
        "</w:r></w:p>"
    )


def make_docx(path: Path, body_text: str | list[str], *, header_text: str | None = None) -> Path:
    if isinstance(body_text, str):
        paragraphs = _paragraph(body_text)
    else:
        paragraphs = "".join(_paragraph(t) for t in body_text)
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body>"
        "</w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", document_xml)
        if header_text is not None:
            header_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"{_paragraph(header_text)}"
                "</w:hdr>"
            )
            zf.writestr("word/header1.xml", header_xml)
    return path


def read_docx_xml(path: Path, name: str = "word/document.xml") -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


def test_description_length():
    skill_md = SCRIPT_PATH.parents[1] / "SKILL.md"
    for line in skill_md.read_text(encoding="utf-8").splitlines():
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"')
            assert desc.endswith(".")
            assert len(desc) <= 60, len(desc)
            return
    raise AssertionError("description: missing from SKILL.md")


def test_strips_han_keeps_english():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("Hello 世界 World")
    assert cleaned == "Hello World"
    assert removed == 2
    assert "世" not in cleaned
    assert "Hello" in cleaned
    assert "World" in cleaned


def test_chinese_only_becomes_empty():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("你好")
    assert cleaned == ""
    assert removed == 2


def test_keeps_western_punctuation_and_digits():
    mod = load_module()
    cleaned, _ = mod.strip_chinese("Price: $12.50 (ok). 价格：一百")
    assert "Price: $12.50 (ok)." in cleaned
    assert "价" not in cleaned
    assert "一" not in cleaned


def test_default_removes_cjk_punctuation():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("Hello。World、next")
    assert cleaned == "HelloWorldnext"
    assert removed == 2


def test_keep_punctuation_flag():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("Hello。世界", keep_punctuation=True)
    assert cleaned == "Hello。"
    assert removed == 2
    assert "。" in cleaned


def test_fullwidth_ascii_converted_not_deleted():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("Ｈｅｌｌｏ，世界")
    assert cleaned == "Hello,"
    assert removed == 2


def test_ideographic_space_becomes_ascii_space():
    mod = load_module()
    cleaned, _ = mod.strip_chinese("Hello\u3000World")
    assert cleaned == "Hello World"


def test_does_not_remove_hiragana_or_hangul():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("こんにちはHello한글中文")
    assert "こんにちは" in cleaned
    assert "한글" in cleaned
    assert "Hello" in cleaned
    assert "中" not in cleaned
    assert removed == 2


def test_numeric_entities_are_stripped():
    mod = load_module()
    cleaned, removed = mod.strip_chinese("Hi&#x4e16;&#19990; there")
    assert cleaned == "Hi there"
    assert removed == 2
    # Non-CJK entities must stay (would break XML if decoded).
    cleaned_xml, _ = mod.strip_chinese("A&#x20;B&#60;tag")
    assert "&#x20;" in cleaned_xml
    assert "&#60;" in cleaned_xml


def test_no_collapse_keeps_double_spaces():
    mod = load_module()
    cleaned, _ = mod.strip_chinese("Hello 世界 World", collapse_spaces=False)
    assert cleaned == "Hello  World"


def test_docx_preserves_xml_indent(tmp_path: Path):
    mod = load_module()
    pretty = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
        "  <w:body>\n"
        "    <w:p><w:r>"
        '<w:t xml:space="preserve">Hello 世界 World</w:t>'
        "</w:r></w:p>\n"
        "  </w:body>\n"
        "</w:document>\n"
    )
    src = tmp_path / "pretty.docx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", pretty)
    dest = tmp_path / "pretty.en.docx"
    mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    xml = read_docx_xml(dest)
    assert "  <w:body>" in xml
    assert ">Hello World<" in xml
    assert "世" not in xml


def test_docx_roundtrip(tmp_path: Path):
    mod = load_module()
    src = make_docx(tmp_path / "mixed.docx", "Agenda 议程 Item 1")
    dest = tmp_path / "mixed.en.docx"
    result = mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    assert result["success"] is True
    assert result["chars_removed"] == 2
    xml = read_docx_xml(dest)
    assert "Agenda Item 1" in xml
    assert "议" not in xml
    assert zipfile.is_zipfile(dest)


def test_docx_header_is_cleaned(tmp_path: Path):
    mod = load_module()
    src = make_docx(
        tmp_path / "headed.docx",
        "Body English",
        header_text="机密 Confidential",
    )
    dest = tmp_path / "headed.en.docx"
    result = mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    assert result["files_changed"] == 1
    assert result["chars_removed"] == 2
    header = read_docx_xml(dest, "word/header1.xml")
    assert "Confidential" in header
    assert "机" not in header


def test_dry_run_does_not_write(tmp_path: Path):
    mod = load_module()
    src = make_docx(tmp_path / "a.docx", "Hello 中文")
    result = mod.process_docx(
        src,
        tmp_path / "a.en.docx",
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=True,
    )
    assert result["dry_run"] is True
    assert result["output"] is None
    assert result["chars_removed"] == 2
    assert not (tmp_path / "a.en.docx").exists()


def test_legacy_doc_rejected(tmp_path: Path):
    mod = load_module()
    src = tmp_path / "old.doc"
    src.write_bytes(b"not a docx")
    with pytest.raises(ValueError, match="Save As .docx"):
        mod.process_path(
            src,
            None,
            keep_punctuation=False,
            collapse_spaces=True,
            dry_run=True,
        )


def test_cli_writes_default_en_suffix(tmp_path: Path, capsys):
    mod = load_module()
    src = make_docx(tmp_path / "report.docx", "Title 标题")
    code = mod.main([str(src)])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    out = Path(payload["output"])
    assert out.name == "report.en.docx"
    assert out.exists()
    assert "Title" in read_docx_xml(out)
    assert "标" not in read_docx_xml(out)
    # Original preserved.
    assert "标" in read_docx_xml(src)


def test_cli_in_place_and_output_conflict(tmp_path: Path, capsys):
    mod = load_module()
    src = make_docx(tmp_path / "x.docx", "Hi")
    code = mod.main([str(src), "--in-place", "-o", str(tmp_path / "y.docx")])
    assert code == 1
    assert "either --in-place or -o" in capsys.readouterr().err


def test_collapse_blank_lines():
    mod = load_module()
    assert mod.collapse_blank_lines("Hello\n\n\nWorld\n") == "Hello\nWorld\n"


def test_docx_removes_empty_paragraphs_left_by_chinese(tmp_path: Path):
    mod = load_module()
    src = make_docx(tmp_path / "lines.docx", ["Hello", "中文段落", "World"])
    dest = tmp_path / "lines.en.docx"
    result = mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    xml = read_docx_xml(dest)
    assert result["empty_paragraphs_removed"] == 1
    assert xml.count("<w:p>") == 2
    assert "Hello" in xml
    assert "World" in xml
    assert "中" not in xml
    # No blank paragraph remaining between the two English lines.
    assert ">Hello</w:t></w:r></w:p><w:p><w:r>" in xml or xml.index("Hello") < xml.index("World")
    empty_paras = re.findall(
        r"<w:p><w:r><w:t xml:space=\"preserve\"></w:t></w:r></w:p>", xml
    )
    assert empty_paras == []


def test_keep_empty_lines_preserves_blank_paragraph(tmp_path: Path):
    mod = load_module()
    src = make_docx(tmp_path / "keep.docx", ["Hello", "中文", "World"])
    dest = tmp_path / "keep.en.docx"
    result = mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
        remove_empty_lines=False,
    )
    xml = read_docx_xml(dest)
    assert result["empty_paragraphs_removed"] == 0
    assert xml.count("<w:p>") == 3


def test_already_stripped_file_drops_blank_lines(tmp_path: Path):
    """Re-run on a file that already lost its Chinese but kept empty paras."""
    mod = load_module()
    src = make_docx(tmp_path / "blank.docx", ["Hello", "", "World"])
    dest = tmp_path / "blank.en.docx"
    result = mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    xml = read_docx_xml(dest)
    assert result["empty_paragraphs_removed"] == 1
    assert xml.count("<w:p>") == 2
    assert "Hello" in xml
    assert "World" in xml


def test_table_cell_keeps_placeholder_paragraph(tmp_path: Path):
    mod = load_module()
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:tbl><w:tr><w:tc>"
        "<w:p><w:r><w:t>中文</w:t></w:r></w:p>"
        "</w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>After</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    src = tmp_path / "table.docx"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("word/document.xml", document_xml)
    dest = tmp_path / "table.en.docx"
    mod.process_docx(
        src,
        dest,
        keep_punctuation=False,
        collapse_spaces=True,
        dry_run=False,
    )
    xml = read_docx_xml(dest)
    assert "<w:tc>" in xml
    assert "<w:p>" in xml.split("<w:tc>", 1)[1].split("</w:tc>", 1)[0]
    assert "After" in xml
    assert "中" not in xml
