---
name: word-strip-chinese
description: Strip Chinese from bilingual Word .docx files.
version: 1.0.0
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
| Word desktop Find & Replace | Ctrl+H → Use wildcards → find `[一-龥]` → replace empty |

## Procedure

1. Confirm the file path. If it is `.doc`, tell the user to save as `.docx`.
2. Run `--dry-run` first when the file is important; report `chars_removed`.
3. Write `file.en.docx` (or `-o` / `--in-place` if the user asked to overwrite).
4. Spot-check with `read_file` on the output `.docx` (Hermes extracts Word text). Confirm English remains and Chinese is gone.
5. Hand the output path back. Do not delete the original unless asked.

### Microsoft Word (no script)

If the user wants to do it inside Word:

1. Ctrl+H (Find and Replace).
2. More → check **Use wildcards**.
3. Find what: `[一-龥]`  (CJK Unified Ideographs).
4. Replace with: leave empty → Replace All.
5. Optionally replace leftover `。` `，` `、` `；` `：` `？` `！` `「` `」` `《` `》` one at a time (wildcards cannot list them all cleanly).

Word wildcards miss Extension-A/B ideographs; the helper script covers those.

### VBA (Word desktop)

```vb
Sub RemoveChineseKeepEnglish()
    Dim i As Long, ch As String, cp As Long
    Dim rng As Range
    Set rng = ActiveDocument.Content
    For i = rng.Characters.Count To 1 Step -1
        ch = rng.Characters(i).Text
        If Len(ch) = 1 Then
            cp = AscW(ch)
            If cp < 0 Then cp = cp + 65536
            If (cp >= &H4E00 And cp <= &H9FFF) Or (cp >= &H3400 And cp <= &H4DBF) Then
                rng.Characters(i).Delete
            End If
        End If
    Next i
End Sub
```

Prefer the Python script for large files; this loop is slow.

## Pitfalls

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
