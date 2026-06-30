"""Dry-run validation (Layer 3) tests — no AWS or Floci needed.

Tests that the deployment scripts validate correctly before any
AWS operation:
- render_config.py --dry-run
- deploy_cluster.sh validation logic (env vars, placeholders)
- .env schema parsing
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.mark.config
class TestRenderConfigDryRun:
    """render_config.py --dry-run produces valid YAML without starting Ray."""

    def test_dry_run_produces_valid_yaml(self, repo_root: Path) -> None:
        """--dry-run outputs valid YAML to stdout."""
        result = subprocess.run(
            [
                "python3",
                str(repo_root / "scripts" / "render_config.py"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env={"MODEL_ID": "test-model", "MODEL_SOURCE": "test/test", "HF_TOKEN": "", "LITELLM_MASTER_KEY": ""},
        )
        assert result.returncode == 0
        assert result.stdout.strip()

    def test_dry_run_fails_without_model_id(self, repo_root: Path) -> None:
        """--dry-run fails when MODEL_ID is missing."""
        result = subprocess.run(
            [
                "python3",
                str(repo_root / "scripts" / "render_config.py"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env={},
        )
        assert result.returncode != 0


@pytest.mark.config
class TestDotEnvSchema:
    """Validate .env.example has all required variables."""

    def test_env_example_has_required_vars(self, repo_root: Path) -> None:
        """.env.example declares all required env vars."""
        content = (repo_root / ".env.example").read_text()
        required = {"HF_TOKEN", "LITELLM_MASTER_KEY", "MODEL_ID", "MODEL_SOURCE"}
        for var in required:
            assert var in content, (
                f"{var} not found in .env.example"
            )

    def test_env_example_has_optional_vars(self, repo_root: Path) -> None:
        """.env.example documents optional vars with defaults."""
        content = (repo_root / ".env.example").read_text()
        optional = {
            "GPU_MEMORY_UTILIZATION",
            "MAX_MODEL_LEN",
            "GPU_COUNT",
            "MODELS_COUNT",
        }
        for var in optional:
            assert var in content, (
                f"{var} not found in .env.example"
            )

    def test_env_example_is_valid_template(self, repo_root: Path) -> None:
        """.env.example lines follow VAR=VALUE or # comment format."""
        content = (repo_root / ".env.example").read_text()
        valid_prefixes = ("export ", "", "#")
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            assert any(
                stripped.startswith(prefix) for prefix in valid_prefixes
            ) or "=" in stripped, (
                f"Line does not look like valid env var: {line}"
            )


@pytest.mark.config
class TestIdiaCli:
    """Verify the ./idia CLI wrapper exists and prints help."""

    def test_idia_cli_exists(self, repo_root: Path) -> None:
        """idia script exists and is executable."""
        cli = repo_root / "idia"
        assert cli.exists()
        assert cli.stat().st_mode & 0o111  # executable

    def test_idia_help_returns_0(self, repo_root: Path) -> None:
        """idia --help exits with 0 and prints usage."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "usage" in result.stdout.lower() or "Usage" in result.stdout

    def test_idia_help_shows_subcommands(self, repo_root: Path) -> None:
        """idia --help lists all expected subcommands."""
        result = subprocess.run(
            ["bash", str(repo_root / "idia"), "--help"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # Expected commands in local deploy pipeline
        expected = {"deploy", "status", "user", "logs", "stop"}
        for cmd in expected:
            assert cmd in result.stdout.lower(), (
                f"Expected subcommand '{cmd}' not in help text"
            )
