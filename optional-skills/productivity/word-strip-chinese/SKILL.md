---
name: word-strip-chinese
description: Strip Chinese from bilingual Word .docx files.
version: 1.0.1
author: eprince999, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Word, docx, Chinese, bilingual, documents, productivity]
    category: productivity
    related_skills: [ocr-and-documents]
---

# Word Strip Chinese Skill

Remove Chinese characters from a bilingual (Chinese + English) Word document and keep the English. This skill does **not** translate; it deletes Han ideographs (and, by default, leftover CJK punctuation such as `。` `，` `、`) while leaving Latin letters, digits, and Western punctuation intact.

Japanese kana and Korean hangul are not removed.

## When to Use

- The user has a mixed Chinese/English `.docx` and wants **only the English** left.
- They ask to 去掉中文, 删除汉字, or "strip Chinese from this Word file".
- Batch-cleaning bilingual notes, worksheets, or meeting minutes.

Do **not** use this when the user wants a translation, a summary, or to keep some Chinese (names, quotes). Ask first if the request is ambiguous.

## Prerequisites

- Python 3.10+ (stdlib only: `zipfile`. No `python-docx`.)
- Input must be `.docx`. Legacy `.doc` must be Save As `.docx` in Word first.

## How to Run

Resolve `scripts/strip_chinese.py` to this skill's absolute path. Run it with the `terminal` tool.

```bash
python3 scripts/strip_chinese.py input.docx
python3 scripts/strip_chinese.py input.docx -o cleaned.docx
python3 scripts/strip_chinese.py input.docx --in-place
python3 scripts/strip_chinese.py input.docx --dry-run
python3 scripts/strip_chinese.py notes.txt
```

Default output is `input.en.docx` next to the source (original is not overwritten). The script prints JSON: `chars_removed`, `files_changed`, `output`.

## Quick Reference

| Goal | Command / action |
|---|---|
| Clean a .docx, keep original | `python3 scripts/strip_chinese.py file.docx` |
| Choose output path | `python3 scripts/strip_chinese.py file.docx -o out.docx` |
| Overwrite | `python3 scripts/strip_chinese.py file.docx --in-place` |
| Count only | `python3 scripts/strip_chinese.py file.docx --dry-run` |
| Keep `。，、`, delete Han only | `python3 scripts/strip_chinese.py file.docx --keep-punctuation` |
| Word Find `[一-龥]` reports 0 | Expected — use VBA or this script, not wildcards |
| Run inside Word | Alt+F11 → import `scripts/RemoveChinese.bas` → F5 |

## Procedure

1. Confirm the file path. If it is `.doc`, tell the user to save as `.docx`.
2. Run `--dry-run` first when the file is important; report `chars_removed`.
3. Write `file.en.docx` (or `-o` / `--in-place` if the user asked to overwrite).
4. Spot-check with `read_file` on the output `.docx` (Hermes extracts Word text). Confirm English remains and Chinese is gone.
5. Hand the output path back. Do not delete the original unless asked.

### Microsoft Word Find reports 0 hits

**`[一-龥]` with Use wildcards is expected to find 0** on most Word builds, especially English-UI Word. The wildcard engine does not treat that as a Unicode Han range. This does not mean the document has no Chinese.

Do **not** keep retrying Find & Replace wildcards. Use one of these:

1. **Sanity check (wildcards OFF):** copy one visible 汉字 from the body, paste it into Find what, Replace All. If that finds hits, the text is real and the range wildcard is the problem.
2. **If that also finds 0:** the glyphs are probably images, text boxes Word's Find skipped, or a locked content control — use the Python script (it reads OOXML) or the VBA below (it also walks headers and shapes).
3. **VBA (inside Word):** `Alt+F11` → `File` → `Import File` → `scripts/RemoveChinese.bas` → click inside `RemoveChineseKeepEnglish` → `F5`. Or Insert → Module and paste the procedures from that file (skip the `Attribute VB_Name` line).
4. **Python (keeps run formatting better):** `python3 scripts/strip_chinese.py file.docx`

Do not check Match whole words / 全字匹配. Do not use wildcards for this job.

### VBA (Word desktop)

The character-by-character `Characters(i).Delete` loop is too slow on long docs. `scripts/RemoveChinese.bas` rewrites paragraph text instead and also covers headers, footers, text boxes, footnotes, and comments.

## Pitfalls

- **Word Find `[一-龥]` → 0 hits is expected** on English-UI Word. The wildcard engine does not honor a Unicode Han range. Use VBA (`RemoveChinese.bas`) or `strip_chinese.py`.
- **Not a translator.** "Hello 世界" becomes "Hello", not "Hello world".
- **Fullwidth ASCII is converted**, not deleted: `Ｈｅｌｌｏ，世界` → `Hello,`.
- **Empty paragraphs** remain if a paragraph was Chinese-only. That is intentional (layout stays).
- **Headers, footers, comments, footnotes** are cleaned (every XML part in the .docx zip). Images are copied unchanged.
- **`--no-collapse`** keeps double spaces left behind by deletions. Default collapse only happens inside Word text runs (`<w:t>`), not in XML indentation.

## Verification

```bash
python3 scripts/strip_chinese.py sample.docx --dry-run
python3 scripts/strip_chinese.py sample.docx -o sample.en.docx
```

Open `sample.en.docx` (or `read_file` it). English, numbers, and commas/periods must remain; Han characters must be gone.
