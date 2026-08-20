---
name: word-strip-chinese
description: Strip Chinese from bilingual Word .docx files.
version: 1.1.0
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

Japanese kana and Korean hangul are not removed. After Han is deleted, **blank paragraphs left by Chinese-only lines are removed by default** so English lines sit next to each other.

## When to Use

- The user has a mixed Chinese/English `.docx` and wants **only the English** left.
- They ask to 去掉中文, 删除汉字, or "strip Chinese from this Word file".
- Batch-cleaning bilingual notes, worksheets, or meeting minutes.

Do **not** use this when the user wants a translation, a summary, or to keep some Chinese (names, quotes). Ask first if the request is ambiguous.

## Prerequisites

- Python 3.10+ (stdlib only: `zipfile`. No `python-docx`.)
- Input must be `.docx`. Legacy `.doc` must be Save As `.docx` in Word first.

## How to Run

The script lives **inside this skill directory**, not under `~/optional-skills/`. If `python3` reports `No such file or directory`, the path is wrong — `$HOME/optional-skills/...` does not exist unless the repo was cloned there.

Resolve the script in this order:

1. **This checkout** (repo root = the directory that contains `optional-skills/`):
   `optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py`
2. **After** `hermes skills install official/productivity/word-strip-chinese`:
   `~/.hermes/skills/productivity/word-strip-chinese/scripts/strip_chinese.py`
3. Copy `scripts/strip_chinese.py` anywhere (Desktop is fine) and pass that path to `python3`.

`input.docx` must be the user's document (often on Desktop or Downloads), not a path next to the script.

```bash
# From the hermes-agent repo root
python3 optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py "/full/path/to/input.docx"

python3 optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py "/full/path/to/input.docx" -o cleaned.docx
python3 optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py "/full/path/to/input.docx" --in-place
python3 optional-skills/productivity/word-strip-chinese/scripts/strip_chinese.py "/full/path/to/input.docx" --dry-run
```

Default output is `input.en.docx` next to the **document**, not next to the script. The script prints JSON: `chars_removed`, `files_changed`, `output`.

## Quick Reference

| Goal | Command / action |
|---|---|
| Clean a .docx, keep original | `python3 <skill>/scripts/strip_chinese.py /full/path/file.docx` |
| Choose output path | `... strip_chinese.py /full/path/file.docx -o out.docx` |
| Overwrite | `... strip_chinese.py /full/path/file.docx --in-place` |
| Count only | `... strip_chinese.py /full/path/file.docx --dry-run` |
| Keep `。，、`, delete Han only | `... strip_chinese.py /full/path/file.docx --keep-punctuation` |
| Keep blank lines from deleted Chinese | `... strip_chinese.py /full/path/file.docx --keep-empty-lines` |
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

- **`python3: can't open file ... No such file`** means the script path is wrong. Do not invent `~/optional-skills/...`. Use the clone path, the installed skill path, or copy `scripts/strip_chinese.py` to `$HOME/strip_chinese.py`.
- **Word Find `[一-龥]` → 0 hits is expected** on English-UI Word. The wildcard engine does not honor a Unicode Han range. Use VBA (`RemoveChinese.bas`) or `strip_chinese.py`.
- **Not a translator.** "Hello 世界" becomes "Hello", not "Hello world".
- **Fullwidth ASCII is converted**, not deleted: `Ｈｅｌｌｏ，世界` → `Hello,`.
- **Empty paragraphs** left by Chinese-only lines are **removed by default**. Pass `--keep-empty-lines` to keep them.
- **Headers, footers, comments, footnotes** are cleaned (every XML part in the .docx zip). Images are copied unchanged.
- **`--no-collapse`** keeps double spaces left behind by deletions. Default collapse only happens inside Word text runs (`<w:t>`), not in XML indentation.

## Verification

```bash
python3 scripts/strip_chinese.py sample.docx --dry-run
python3 scripts/strip_chinese.py sample.docx -o sample.en.docx
```

Open `sample.en.docx` (or `read_file` it). English, numbers, and commas/periods must remain; Han characters must be gone.
