"""Shared fixtures for the IDIA Server test suite.

All test categories (docs, config, integration, security, aws) share
the repo_root fixture. Category-specific fixtures live in the
corresponding test module.

AWS/Floci fixtures are session-scoped and shared across all aws-marked
tests. They depend on testcontainers-floci and will skip if the package
is not installed or Docker is not available.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return Path(__file__).parent.parent.resolve()


@pytest.fixture
def docs_dir(repo_root: Path) -> Path:
    """Path to the docs/ directory."""
    return repo_root / "docs"


@pytest.fixture
def config_files(repo_root: Path) -> dict[str, Path]:
    """Map of logical config names to their expected file paths.

    Files that do not yet exist (to be created in later phases) are
    still listed so tests can clearly distinguish "file absent" from
    "file missing". Tests SHOULD skip with pytest.skip() rather than
    fail when a Phase 2+ file is not yet present.
    """
    return {
        "agents": repo_root / "AGENTS.md",
        "dockerfile_ray": repo_root / "Dockerfile.ray",
        "serve_config": repo_root / "serve_config.yaml",
        "docker_compose": repo_root / "docker-compose.yml",
        "config_litellm": repo_root / "config.yaml",
        "cluster": repo_root / "cluster.yaml",
        "prometheus": repo_root / "prometheus.yml",
        "env_example": repo_root / ".env.example",
    }


# ── AWS (Floci) fixtures ────────────────────────────────────────────────


@pytest.fixture(scope="session")
def floci():
    """Start a Floci container and yield it for the full test session.

    Floci emulates 41 AWS services (S3, EC2, IAM, STS, etc.) on a single
    endpoint. The container starts once and is reused by all aws-marked
    tests.
    """
    floci = pytest.importorskip("floci")
    FlociContainer = floci.FlociContainer
    with FlociContainer(image="floci/floci:latest") as container:
        yield container


@pytest.fixture(scope="session")
def _aws_client_kwargs(floci) -> dict[str, Any]:
    """Common keyword arguments for all boto3 clients pointed at Floci."""
    return dict(
        endpoint_url=floci.get_endpoint(),
        region_name=floci.get_region(),
        aws_access_key_id=floci.get_access_key(),
        aws_secret_access_key=floci.get_secret_key(),
    )


@pytest.fixture(scope="session")
def s3_client(_aws_client_kwargs) -> Any:
    """boto3 S3 client pre-configured for Floci (path-style addressing)."""
    import boto3
    from botocore.config import Config

    kwargs = dict(_aws_client_kwargs)
    kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


@pytest.fixture(scope="session")
def ec2_client(_aws_client_kwargs) -> Any:
    """boto3 EC2 client pre-configured for Floci."""
    import boto3

    return boto3.client("ec2", **_aws_client_kwargs)


@pytest.fixture(scope="session")
def iam_client(_aws_client_kwargs) -> Any:
    """boto3 IAM client pre-configured for Floci."""
    import boto3

    return boto3.client("iam", **_aws_client_kwargs)


@pytest.fixture(scope="session")
def sts_client(_aws_client_kwargs) -> Any:
    """boto3 STS client pre-configured for Floci."""
    import boto3

    return boto3.client("sts", **_aws_client_kwargs)


@pytest.fixture(scope="session")
def aws_script_env(floci) -> dict[str, str]:
    """Environment variables for running shell scripts against Floci.

    Includes PATH explicitly (captured during fixture setup, when PATH is
    still intact) as a backup in case os.environ is modified by a test
    plugin or Docker interaction before test execution.
    """
    return {
        "AWS_ENDPOINT_URL": floci.get_endpoint(),
        "AWS_DEFAULT_REGION": floci.get_region(),
        "AWS_ACCESS_KEY_ID": floci.get_access_key(),
        "AWS_SECRET_ACCESS_KEY": floci.get_secret_key(),
        "AWS_PAGER": "",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
    }
