"""SKILL.md validation tests."""
from __future__ import annotations

from pathlib import Path

from mega_tron.pre_flight import (
    CODEX_DESC_MAX,
    codex_compat_error,
    normalize_desc,
    parse_frontmatter,
    validate,
)


def _write_skill(tmp: Path, name: str, body: str) -> Path:
    folder = tmp / name
    folder.mkdir()
    skill_md = folder / "SKILL.md"
    skill_md.write_text(body, encoding="utf-8")
    return skill_md


def test_parse_frontmatter_valid(tmp_path: Path) -> None:
    md = _write_skill(
        tmp_path,
        "ok",
        """---
name: ok
description: a valid description
---

body
""",
    )
    fm, err = parse_frontmatter(md)
    assert err is None
    assert fm == {"name": "ok", "description": "a valid description"}


def test_parse_frontmatter_missing(tmp_path: Path) -> None:
    md = _write_skill(tmp_path, "no-front", "# Just markdown, no YAML.\n")
    fm, err = parse_frontmatter(md)
    assert fm == {}
    assert err == "no YAML frontmatter"


def test_parse_frontmatter_invalid_yaml(tmp_path: Path) -> None:
    md = _write_skill(
        tmp_path,
        "bad-yaml",
        """---
name: bad
description: "unterminated
---

body
""",
    )
    fm, err = parse_frontmatter(md)
    assert fm == {}
    assert err is not None and err.startswith("invalid YAML")


def test_codex_compat_error_passes_string() -> None:
    assert codex_compat_error({"description": "a normal string"}) is None


def test_codex_compat_error_missing_description() -> None:
    assert codex_compat_error({"name": "x"}) == "missing description"


def test_description_error_accepts_list() -> None:
    """Lists are tolerated at admission — normalize_desc joins them at
    embed time. (Earlier revisions rejected lists outright to mirror
    Codex's spec; mega-tron is host-agnostic and routes ≤10 picks/turn
    so the Codex cap doesn't apply at admission.)"""
    err = codex_compat_error({"description": ["a", "list"]})
    assert err is None


def test_description_error_long_string_is_admitted() -> None:
    """Description length is NOT an admission check. mega-tron routes
    top-K only (≤10 picks/turn), so the Codex 1024-char inline cap is
    irrelevant here — it gets applied at stage time by the Codex
    prepender, not at the router gate. BGE-M3 handles >8K context just
    fine on the embed side."""
    long = "x" * (CODEX_DESC_MAX + 1)
    err = codex_compat_error({"description": long})
    assert err is None


def test_description_error_rejects_empty_string() -> None:
    err = codex_compat_error({"description": "   "})
    assert err is not None and "empty" in err.lower()


def test_description_error_rejects_missing() -> None:
    err = codex_compat_error({})
    assert err is not None and "missing" in err.lower()


def test_normalize_desc_collapses_whitespace() -> None:
    assert normalize_desc("  a\n\tb   c  ") == "a b c"


def test_normalize_desc_joins_list() -> None:
    assert normalize_desc(["one", "two", "three"]) == "one two three"


def test_normalize_desc_none() -> None:
    assert normalize_desc(None) == ""


def test_validate_end_to_end_pass(tmp_path: Path) -> None:
    md = _write_skill(
        tmp_path,
        "ok",
        """---
name: ok
description: fine
---
""",
    )
    assert validate(md) is None


def test_validate_end_to_end_fail(tmp_path: Path) -> None:
    md = _write_skill(
        tmp_path,
        "bad",
        """---
name: bad
description: 123
---
""",
    )
    err = validate(md)
    assert err is not None
    assert "must be string" in err.reason
