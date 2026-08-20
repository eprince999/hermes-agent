#!/usr/bin/env python3
"""Strip Chinese (Han + CJK punctuation) from bilingual Word documents.

Keeps English letters, digits, and Western punctuation. Uses only the
Python standard library (zipfile) so .docx files do not need python-docx.

Usage:
  python strip_chinese.py input.docx
  python strip_chinese.py input.docx -o cleaned.docx
  python strip_chinese.py input.docx --in-place
  python strip_chinese.py notes.txt --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from pathlib import Path

# CJK Unified Ideographs + extensions + compatibility + radicals + bopomofo.
# Intentionally excludes Hiragana, Katakana, and Hangul — those are not Chinese.
_HAN_RE = re.compile(
    "["
    "\u2e80-\u2eff"  # CJK Radicals Supplement
    "\u2f00-\u2fdf"  # Kangxi Radicals
    "\u2ff0-\u2fff"  # Ideographic Description Characters
    "\u3100-\u312f"  # Bopomofo
    "\u31a0-\u31bf"  # Bopomofo Extended
    "\u31c0-\u31ef"  # CJK Strokes
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\U00020000-\U0002a6df"  # Extension B
    "\U0002a700-\U0002b73f"  # Extension C
    "\U0002b740-\U0002b81f"  # Extension D
    "\U0002b820-\U0002ceaf"  # Extension E
    "\U0002ceb0-\U0002ebef"  # Extension F
    "\U0002f800-\U0002fa1f"  # Compatibility Ideographs Supplement
    "\U00030000-\U0003134f"  # Extension G
    "]+"
)

# Ideographic punctuation that remains after fullwidth-ASCII conversion.
# U+3000 (ideographic space) is converted to ASCII space, not deleted.
_PUNCT_RE = re.compile(
    "["
    "\u3001-\u303f"  # CJK Symbols and Punctuation (skip U+3000)
    "\ufe10-\ufe1f"  # Vertical forms
    "\ufe30-\ufe4f"  # CJK Compatibility Forms
    "]+"
)

_ENTITY_RE = re.compile(r"&#x([0-9A-Fa-f]{1,6});|&#([0-9]{1,7});")

_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".html", ".htm", ".xml"}
_XML_PART_SUFFIXES = (".xml", ".rels")
# w:t / a:t (and similar) text nodes. CJK never appears in element names, but
# collapsing spaces must stay inside these nodes so XML indentation is kept.
_XML_T_RE = re.compile(
    r"(<(?:[\w.-]+:)?t\b[^>]*>)(.*?)(</(?:[\w.-]+:)?t>)",
    re.DOTALL,
)
_W_P_RE = re.compile(
    r"<(?:[\w.-]+:)?p\b[^>]*/>|<(?:[\w.-]+:)?p\b[^>]*>.*?</(?:[\w.-]+:)?p\s*>",
    re.DOTALL,
)
_KEEP_PARA_RE = re.compile(
    r"<(?:[\w.-]+:)?(?:drawing|pict|object|tbl|sectPr)\b"
    r"|w:type=['\"]page['\"]"
    r"|type=['\"]page['\"]"
)


def _codepoint_is_han(cp: int) -> bool:
    return bool(_HAN_RE.fullmatch(chr(cp))) if 0 <= cp <= 0x10FFFF else False


def _codepoint_is_cjk_punct(cp: int) -> bool:
    return bool(_PUNCT_RE.fullmatch(chr(cp))) if 0 <= cp <= 0x10FFFF else False


def fullwidth_to_ascii(text: str) -> str:
    """Convert fullwidth ASCII (！Ａ０ etc.) and ideographic space to ASCII.

    Fullwidth punctuation such as ，．！ becomes , . ! rather than being
    deleted, so English sentences keep readable separators.
    """
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - 0xFEE0))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _strip_cjk_numeric_entities(text: str, *, keep_punctuation: bool) -> tuple[str, int]:
    """Remove numeric character references that encode Chinese codepoints."""
    removed = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal removed
        cp = int(match.group(1), 16) if match.group(1) else int(match.group(2))
        if _codepoint_is_han(cp) or (not keep_punctuation and _codepoint_is_cjk_punct(cp)):
            removed += 1
            return ""
        return match.group(0)

    return _ENTITY_RE.sub(repl, text), removed


def collapse_xml_text_node_spaces(xml: str) -> str:
    """Collapse ASCII spaces only inside ``<w:t>`` / ``<a:t>`` nodes."""

    def repl(match: re.Match[str]) -> str:
        inner = re.sub(r"[ \t]{2,}", " ", match.group(2))
        return f"{match.group(1)}{inner}{match.group(3)}"

    return _XML_T_RE.sub(repl, xml)


def collapse_blank_lines(text: str) -> str:
    """Turn leftover empty lines (Chinese-only paragraphs) into a single newline."""
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def paragraph_is_visually_blank(p_xml: str) -> bool:
    """True when a ``w:p`` would render as an empty line in Word."""
    if _KEEP_PARA_RE.search(p_xml):
        return False
    visible = "".join(m.group(2) for m in _XML_T_RE.finditer(p_xml))
    return visible.strip() == ""


def _table_spans(xml: str) -> list[tuple[int, int]]:
    """Byte offsets of ``w:tbl`` blocks, including nested tables."""
    open_pat = re.compile(r"<(?:[\w.-]+:)?tbl\b[^>]*?(/>|>)")
    close_pat = re.compile(r"</(?:[\w.-]+:)?tbl\s*>")
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        start = open_pat.search(xml, pos)
        if not start:
            break
        if start.group(0).endswith("/>"):
            spans.append(start.span())
            pos = start.end()
            continue
        depth = 1
        cursor = start.end()
        while depth:
            nxt_open = open_pat.search(xml, cursor)
            nxt_close = close_pat.search(xml, cursor)
            if not nxt_close:
                break
            if nxt_open and nxt_open.start() < nxt_close.start():
                if not nxt_open.group(0).endswith("/>"):
                    depth += 1
                cursor = nxt_open.end()
            else:
                depth -= 1
                cursor = nxt_close.end()
        spans.append((start.start(), cursor))
        pos = cursor
    return spans


def _in_spans(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in spans)


def remove_empty_paragraphs(xml: str) -> tuple[str, int]:
    """Delete blank ``w:p`` lines left after Chinese-only paragraphs are stripped.

    Paragraphs inside tables are kept: a cell must contain at least one ``w:p``.
    """
    if "<" not in xml:
        return xml, 0
    protected = _table_spans(xml)
    chunks: list[str] = []
    last = 0
    removed = 0
    for match in _W_P_RE.finditer(xml):
        if _in_spans(match.start(), protected):
            continue
        if not paragraph_is_visually_blank(match.group(0)):
            continue
        chunks.append(xml[last:match.start()])
        last = match.end()
        removed += 1
    if removed == 0:
        return xml, 0
    chunks.append(xml[last:])
    return "".join(chunks), removed


def _sub_count_chars(pattern: re.Pattern[str], text: str) -> tuple[str, int]:
    """Delete matches and count deleted characters (not regex-match count)."""
    removed = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal removed
        removed += len(match.group(0))
        return ""

    return pattern.sub(repl, text), removed


def strip_chinese(
    text: str,
    *,
    keep_punctuation: bool = False,
    collapse_spaces: bool = True,
) -> tuple[str, int]:
    """Return ``(cleaned_text, chars_removed)``.

    ``chars_removed`` counts Han (and CJK punctuation, unless kept). Fullwidth
    ASCII conversion does not count as removal.
    """
    text = fullwidth_to_ascii(text)
    text, entity_removed = _strip_cjk_numeric_entities(
        text, keep_punctuation=keep_punctuation
    )
    text, han_removed = _sub_count_chars(_HAN_RE, text)
    punct_removed = 0
    if not keep_punctuation:
        text, punct_removed = _sub_count_chars(_PUNCT_RE, text)
    if collapse_spaces:
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text, han_removed + punct_removed + entity_removed


def default_output_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}.en{source.suffix}")


def process_text_file(
    source: Path,
    dest: Path | None,
    *,
    keep_punctuation: bool,
    collapse_spaces: bool,
    dry_run: bool,
    remove_empty_lines: bool = True,
) -> dict:
    original = source.read_text(encoding="utf-8")
    cleaned, removed = strip_chinese(
        original,
        keep_punctuation=keep_punctuation,
        collapse_spaces=collapse_spaces,
    )
    empty_removed = 0
    if remove_empty_lines:
        collapsed = collapse_blank_lines(cleaned)
        empty_removed = cleaned.count("\n") - collapsed.count("\n")
        cleaned = collapsed
    dest = dest or default_output_path(source)
    changed = cleaned != original
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(cleaned, encoding="utf-8")
    return {
        "success": True,
        "input": str(source),
        "output": None if dry_run else str(dest),
        "chars_removed": removed,
        "empty_paragraphs_removed": max(empty_removed, 0),
        "files_changed": 1 if changed else 0,
        "dry_run": dry_run,
    }


def _rewrite_docx_part(
    filename: str,
    data: bytes,
    *,
    keep_punctuation: bool,
    collapse_spaces: bool,
    remove_empty_lines: bool,
) -> tuple[bytes, int, bool, int]:
    if not filename.lower().endswith(_XML_PART_SUFFIXES):
        return data, 0, False, 0
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0, False, 0
    cleaned, removed = strip_chinese(
        text,
        keep_punctuation=keep_punctuation,
        collapse_spaces=False,
    )
    if collapse_spaces:
        cleaned = collapse_xml_text_node_spaces(cleaned)
    empty_removed = 0
    if remove_empty_lines and filename.lower().endswith(".xml"):
        cleaned, empty_removed = remove_empty_paragraphs(cleaned)
    if cleaned == text:
        return data, 0, False, 0
    return cleaned.encode("utf-8"), removed, True, empty_removed


def process_docx(
    source: Path,
    dest: Path | None,
    *,
    keep_punctuation: bool,
    collapse_spaces: bool,
    dry_run: bool,
    remove_empty_lines: bool = True,
) -> dict:
    if not zipfile.is_zipfile(source):
        raise ValueError(f"Not a valid .docx (zip) file: {source}")

    dest = dest or default_output_path(source)
    total_removed = 0
    empty_removed = 0
    files_changed = 0
    rewritten: list[tuple[zipfile.ZipInfo, bytes]] = []

    with zipfile.ZipFile(source, "r") as zin:
        for info in zin.infolist():
            data = zin.read(info.filename)
            new_data, removed, changed, empty_n = _rewrite_docx_part(
                info.filename,
                data,
                keep_punctuation=keep_punctuation,
                collapse_spaces=collapse_spaces,
                remove_empty_lines=remove_empty_lines,
            )
            if changed:
                files_changed += 1
                total_removed += removed
                empty_removed += empty_n
            rewritten.append((info, new_data))

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_dest = dest
        if dest.resolve() == source.resolve():
            tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        with zipfile.ZipFile(tmp_dest, "w") as zout:
            for info, data in rewritten:
                new_info = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                new_info.compress_type = zipfile.ZIP_DEFLATED
                new_info.external_attr = info.external_attr
                zout.writestr(new_info, data)
        if tmp_dest != dest:
            tmp_dest.replace(dest)

    return {
        "success": True,
        "input": str(source),
        "output": None if dry_run else str(dest),
        "chars_removed": total_removed,
        "empty_paragraphs_removed": empty_removed,
        "files_changed": files_changed,
        "dry_run": dry_run,
    }


def process_path(
    source: Path,
    dest: Path | None,
    *,
    keep_punctuation: bool,
    collapse_spaces: bool,
    dry_run: bool,
    remove_empty_lines: bool = True,
) -> dict:
    suffix = source.suffix.lower()
    if suffix == ".doc":
        raise ValueError(
            "Legacy .doc is not supported. Open it in Word and Save As .docx first."
        )
    if suffix == ".docx":
        return process_docx(
            source,
            dest,
            keep_punctuation=keep_punctuation,
            collapse_spaces=collapse_spaces,
            dry_run=dry_run,
            remove_empty_lines=remove_empty_lines,
        )
    if suffix in _TEXT_EXTENSIONS or suffix == "":
        return process_text_file(
            source,
            dest,
            keep_punctuation=keep_punctuation,
            collapse_spaces=collapse_spaces,
            dry_run=dry_run,
            remove_empty_lines=remove_empty_lines,
        )
    raise ValueError(f"Unsupported file type: {suffix or source.name}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove Chinese characters from bilingual Word/text files, keeping English."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="Input .docx or text file(s)")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file (single input) or directory (multiple inputs)",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file instead of writing <stem>.en<suffix>",
    )
    parser.add_argument(
        "--keep-punctuation",
        action="store_true",
        help="Keep CJK punctuation (。，、) and only delete Han characters",
    )
    parser.add_argument(
        "--no-collapse",
        action="store_true",
        help="Do not collapse leftover ASCII spaces after deletions",
    )
    parser.add_argument(
        "--keep-empty-lines",
        action="store_true",
        help="Keep blank paragraphs left behind after Chinese-only lines are deleted",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count removals without writing an output file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    sources = [p.expanduser() for p in args.inputs]
    missing = [p for p in sources if not p.is_file()]
    if missing:
        print(f"File not found: {missing[0]}", file=sys.stderr)
        return 1
    if args.in_place and args.output:
        print("Use either --in-place or -o, not both.", file=sys.stderr)
        return 1
    if args.output and len(sources) > 1 and not args.output.is_dir():
        args.output.mkdir(parents=True, exist_ok=True)

    results = []
    try:
        for source in sources:
            if args.in_place:
                dest = source
            elif args.output is None:
                dest = None
            elif len(sources) == 1 and not args.output.is_dir():
                dest = args.output
            else:
                dest = args.output / default_output_path(source).name
            results.append(
                process_path(
                    source,
                    dest,
                    keep_punctuation=args.keep_punctuation,
                    collapse_spaces=not args.no_collapse,
                    dry_run=args.dry_run,
                    remove_empty_lines=not args.keep_empty_lines,
                )
            )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = results[0] if len(results) == 1 else {"success": True, "files": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
