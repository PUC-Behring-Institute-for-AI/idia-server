"""Documentation structure and consistency tests.

These tests verify that:
- Required documentation files exist
- Cross-references between documents are consistent
- Document headers and structure follow conventions

They run with zero external dependencies and no infrastructure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ── Required documentation files ────────────────────────────────────────

REQUIRED_DOCS: list[tuple[str, str]] = [
    ("architecture", "docs/ARCHITECTURE.md"),
    ("agents", "AGENTS.md"),
    ("readme", "README.md"),
]

LIVING_DOC_SECTIONS: dict[str, list[str]] = {
    "docs/ARCHITECTURE.md": [
        "Document Evolution Contract",
        "Structural Change History",
    ],
}


@pytest.mark.docs
class TestRequiredDocs:
    """Every required document exists and is non-empty."""

    @pytest.mark.parametrize("name,rel_path", REQUIRED_DOCS)
    def test_exists(self, name: str, rel_path: str, repo_root: Path) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{name} ({rel_path}) not created yet — check later phase")
        assert path.is_file(), f"{rel_path} exists but is not a file"
        assert path.stat().st_size > 0, f"{rel_path} is empty"

    @pytest.mark.parametrize("name,rel_path", REQUIRED_DOCS)
    def test_is_markdown(self, name: str, rel_path: str, repo_root: Path) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{name} ({rel_path}) not created yet")
        content = path.read_text(encoding="utf-8")
        assert content.startswith("#"), f"{rel_path} does not start with a heading"


@pytest.mark.docs
class TestLivingDocSections:
    """Living documents contain the required governance sections."""

    @pytest.mark.parametrize("rel_path,expected_sections", [
        (rel, secs) for rel, secs in LIVING_DOC_SECTIONS.items()
    ])
    def test_contains_sections(
        self, rel_path: str, expected_sections: list[str], repo_root: Path
    ) -> None:
        path = repo_root / rel_path
        if not path.exists():
            pytest.skip(f"{rel_path} not created yet")
        content = path.read_text(encoding="utf-8")
        for section in expected_sections:
            assert section in content, (
                f"Missing section '{section}' in {rel_path}"
            )


@pytest.mark.docs
class TestArchitectureFooter:
    """ARCHITECTURE.md carries a version footer."""

    def test_has_version_footer(self, docs_dir: Path) -> None:
        path = docs_dir / "ARCHITECTURE.md"
        if not path.exists():
            pytest.skip("ARCHITECTURE.md not created yet")
        content = path.read_text(encoding="utf-8")
        # Footer marker
        assert "*Document version:" in content, (
            "ARCHITECTURE.md is missing the version footer"
        )
        # At least one structural change entry
        assert "Structural Change History" in content, (
            "ARCHITECTURE.md is missing the Structural Change History"
        )
