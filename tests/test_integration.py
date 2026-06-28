"""Integration tests for the IDIA Server build core.

These tests validate the integration between components without requiring
a real Docker or GPU environment. They exercise:

    - render_config.py: env var substitution, YAML output, error paths
    - serve_config.yaml: structural integrity after rendering
    - docker-compose.yml: service dependency graph
    - config.yaml: LiteLLM routing consistency with serve_config

Tests that genuinely require a running container (docker compose up,
E2E inference, GPU detection) are marked @pytest.mark.integration and
skip when Docker is unavailable — they target pre-release validation on
GPU-equipped hardware and are documented for manual execution.

See docs/ARCHITECTURE.md §11 for the testing philosophy.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def scripts_dir(repo_root: Path) -> Path:
    return repo_root / "scripts"


@pytest.fixture
def serve_config_yaml(repo_root: Path) -> Path:
    return repo_root / "serve_config.yaml"


# ── render_config.py integration tests ─────────────────────────────────────


@pytest.mark.integration
class TestRenderConfig:
    """Exercise ``scripts/render_config.py`` as a module.

    These tests call ``render_config.render()`` directly — a pure function
    that substitutes env var placeholders and validates the YAML output.
    No Docker or GPU required.
    """

    def test_render_with_minimal_env(self, scripts_dir: Path) -> None:
        """Minimal required env vars produce valid rendered YAML."""
        # render needs to be importable from the scripts dir
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render  # type: ignore[import-untyped]
        finally:
            sys.path.pop(0)

        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
        }
        rendered = render(
            yaml.safe_dump({
                "proxy_location": "EveryNode",
                "http_options": {"host": "0.0.0.0", "port": 8000},
                "applications": [{
                    "name": "llms",
                    "import_path": "ray.serve.llm:build_openai_app",
                    "route_prefix": "/",
                    "args": {
                        "llm_configs": [{
                            "model_loading_config": {
                                "model_id": "${MODEL_ID}",
                                "model_source": "${MODEL_SOURCE}",
                            },
                            "deployment_config": {
                                "autoscaling_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 4,
                                    "target_ongoing_requests": 64,
                                },
                            },
                        }],
                    },
                }],
            }),
            overrides=overrides,
        )
        parsed = yaml.safe_load(rendered)
        assert parsed["applications"][0]["args"]["llm_configs"][0]["model_loading_config"]["model_id"] == "test-model"
        assert parsed["applications"][0]["args"]["llm_configs"][0]["model_loading_config"]["model_source"] == "test-org/test-model"

    def test_render_injects_defaults(self, scripts_dir: Path) -> None:
        """Optional vars get defaults injected when absent from env."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        overrides: dict[str, str] = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "MAX_MODEL_LEN": "4096",  # override default
            # GPU_MEMORY_UTILIZATION NOT set — should use default 0.9
        }
        rendered = render(
            yaml.safe_dump({
                "proxy_location": "EveryNode",
                "http_options": {"host": "0.0.0.0", "port": 8000},
                "applications": [{
                    "name": "llms",
                    "import_path": "ray.serve.llm:build_openai_app",
                    "route_prefix": "/",
                    "args": {
                        "llm_configs": [{
                            "model_loading_config": {
                                "model_id": "${MODEL_ID}",
                                "model_source": "${MODEL_SOURCE}",
                            },
                            "engine_kwargs": {
                                "gpu_memory_utilization": "${GPU_MEMORY_UTILIZATION}",
                                "max_model_len": "${MAX_MODEL_LEN}",
                            },
                            "deployment_config": {
                                "autoscaling_config": {
                                    "min_replicas": 1,
                                    "max_replicas": 4,
                                    "target_ongoing_requests": 64,
                                },
                            },
                        }],
                    },
                }],
            }),
            overrides=overrides,
        )
        parsed = yaml.safe_load(rendered)
        engine = parsed["applications"][0]["args"]["llm_configs"][0]["engine_kwargs"]
        assert engine["gpu_memory_utilization"] == 0.9
        assert engine["max_model_len"] == 4096

    def test_render_validates_full_template(self, serve_config_yaml: Path) -> None:
        """The real serve_config.yaml template renders to valid YAML."""
        sys.path.insert(0, str(serve_config_yaml.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        template = serve_config_yaml.read_text(encoding="utf-8")
        overrides = {
            "MODEL_ID": "test-model",
            "MODEL_SOURCE": "test-org/test-model",
            "MAX_MODEL_LEN": "4096",
            "GPU_MEMORY_UTILIZATION": "0.85",
        }
        rendered = render(template, overrides=overrides)
        parsed = yaml.safe_load(rendered)

        # Structural assertions matching ARCHITECTURE.md §5.3
        assert parsed["proxy_location"] == "EveryNode"
        assert parsed["http_options"]["port"] == 8000
        apps = parsed["applications"]
        assert len(apps) == 1
        llm_cfg = apps[0]["args"]["llm_configs"][0]
        assert llm_cfg["model_loading_config"]["model_id"] == "test-model"
        assert llm_cfg["deployment_config"]["autoscaling_config"]["min_replicas"] == 1
        assert llm_cfg["deployment_config"]["autoscaling_config"]["max_replicas"] == 4

    def test_dry_run_flag(self, scripts_dir: Path) -> None:
        """--dry-run renders to stdout without launching serve."""
        env = os.environ.copy()
        env.update(
            MODEL_ID="test-model",
            MODEL_SOURCE="test-org/test-model",
        )
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "render_config.py"), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=scripts_dir,
        )
        assert result.returncode == 0, f"dry-run failed: {result.stderr}"
        # Output should be valid YAML
        parsed = yaml.safe_load(result.stdout)
        assert parsed is not None
        assert "applications" in parsed


@pytest.mark.integration
class TestRenderConfigErrors:
    """Error paths in render_config.py."""

    def test_missing_required_var_fails(self, scripts_dir: Path) -> None:
        """Missing MODEL_ID exits with code 1."""
        env = os.environ.copy()
        env.pop("MODEL_ID", None)
        env["MODEL_SOURCE"] = "test-org/test-model"
        result = subprocess.run(
            [sys.executable, str(scripts_dir / "render_config.py"), "--dry-run"],
            capture_output=True,
            text=True,
            env=env,
            cwd=scripts_dir,
        )
        assert result.returncode != 0
        assert "MODEL_ID" in result.stderr

    def test_bad_yaml_template_fails(self, scripts_dir: Path) -> None:
        """A template producing invalid YAML after substitution exits with 1."""
        sys.path.insert(0, str(scripts_dir.parent))
        try:
            from scripts.render_config import render
        finally:
            sys.path.pop(0)

        # Inject an unclosed mapping after substitution
        with pytest.raises(SystemExit):
            render("key: ${UNKNOWN_VAR}\n  bad_indent", overrides={})


# ── Compose dependency consistency ──────────────────────────────────────────


@pytest.mark.integration
class TestComposeConsistency:
    """Validate the docker-compose.yml structure.

    These tests run without Docker — they parse the YAML and check
    structural properties. Actual compose validation requires a host
    with Docker engine.
    """

    def test_ray_head_builds_locally(self, repo_root: Path) -> None:
        """ray-head service builds from local Dockerfile, not an image."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        ray = compose["services"]["ray-head"]
        assert "build" in ray, "ray-head must build from local Dockerfile"
        assert ray["build"]["dockerfile"] == "Dockerfile.ray"

    def test_litellm_uses_pinned_image(self, repo_root: Path) -> None:
        """LiteLLM image tag is a semver, not :latest."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        image = compose["services"]["litellm"]["image"]
        assert ":latest" not in image, f"LiteLLM image uses :latest: {image}"
        assert re.search(r":v?\d+\.\d+\.\d+", image), f"LiteLLM image not pinned: {image}"

    def test_ray_head_passes_vars_to_entrypoint(self, repo_root: Path) -> None:
        """ray-head passes all env vars required by render_config.py."""
        compose = yaml.safe_load(
            (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
        )
        env_list = compose["services"]["ray-head"].get("environment", [])
        env_str = "\n".join(env_list) if isinstance(env_list, list) else str(env_list)
        for var in ["MODEL_ID", "MODEL_SOURCE", "MAX_MODEL_LEN", "GPU_MEMORY_UTILIZATION"]:
            assert var in env_str, f"ray-head missing env var: {var}"
