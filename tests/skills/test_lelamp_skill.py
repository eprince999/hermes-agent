"""Skill-file contracts for the bundled LeLamp skill."""

from __future__ import annotations

import re
from pathlib import Path

SKILL = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "smart-home"
    / "lelamp"
    / "SKILL.md"
)


def test_description_is_one_short_sentence():
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"^description: (.*)$", text, re.MULTILINE)
    assert match is not None
    description = match.group(1).strip().strip('"')
    assert description.endswith(".")
    assert len(description) <= 60
    assert "powerful" not in description.lower()
    assert "comprehensive" not in description.lower()


def test_body_names_native_tools_not_shell_utils():
    text = SKILL.read_text(encoding="utf-8")
    assert "`lelamp_express`" in text
    assert "`lelamp_light`" in text
    assert "`lelamp_status`" in text
    assert "`memory`" in text
    assert "`terminal`" in text
    # Do not tell the model to grep/cat/sed the lamp.
    assert "`grep`" not in text
    assert "`cat`" not in text
    assert "`sed`" not in text


def test_modern_section_order_is_present():
    text = SKILL.read_text(encoding="utf-8")
    headings = re.findall(r"^## .+$", text, re.MULTILINE)
    names = [h[3:].strip() for h in headings]
    expected = [
        "When to Use",
        "Prerequisites",
        "How to Run",
        "Quick Reference",
        "Procedure",
        "Pitfalls",
        "Verification",
    ]
    assert names[: len(expected)] == expected
