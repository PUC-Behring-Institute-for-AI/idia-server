"""Configuration file schema validation tests.

These tests validate that infrastructure configuration files
(Dockerfile.ray, serve_config.yaml, docker-compose.yml, config.yaml,
cluster.yaml, .env.example, prometheus.yml) conform to the structure
defined in docs/ARCHITECTURE.md.

Each test skips gracefully if the target file has not been created
yet (later phase), so this module is safe to run from Phase 1 onward.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


# ── Helpers ─────────────────────────────────────────────────────────────

def _read_yaml(path: Path) -> dict | None:
    """Return parsed YAML, or None if file is absent."""
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    return yaml.safe_load(raw)


def _has_keys(obj: dict, *keys: str) -> bool:
    """Check that *keys exist at the top level of *obj*."""
    return all(k in obj for k in keys)


# ── serve_config.yaml (§5.3) ────────────────────────────────────────────

SERVE_CONFIG_REQUIRED_KEYS = [
    "proxy_location",
    "http_options",
    "applications",
]


@pytest.mark.config
class TestServeConfig:
    """serve_config.yaml structure per §5.3 of the architecture."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["serve_config"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        for key in SERVE_CONFIG_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_proxy_location(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        assert config.get("proxy_location") == "EveryNode"

    def test_http_options_host(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        opts = config.get("http_options", {})
        # Per §9.2: binds 0.0.0.0 inside the container only
        assert opts.get("host") == "0.0.0.0"

    def test_http_options_port(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        opts = config.get("http_options", {})
        assert opts.get("port") == 8000

    def test_applications_is_list(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        apps = config.get("applications", [])
        assert isinstance(apps, list), "applications must be a list"
        assert len(apps) > 0, "at least one application required"

    def test_first_app_has_route_prefix(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("serve_config.yaml not created yet")
        apps = config.get("applications", [])
        if not apps:
            pytest.skip("no applications defined yet")
        assert "/" in apps[0].get("route_prefix", "")


# ── docker-compose.yml (§5.4, §10.2) ────────────────────────────────────

COMPOSE_REQUIRED_SERVICES = ["ray-head", "litellm"]


@pytest.mark.config
class TestDockerCompose:
    """docker-compose.yml structure per §5.4 and §10.2."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["docker_compose"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_services(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        services = config.get("services", {})
        for svc in COMPOSE_REQUIRED_SERVICES:
            assert svc in services, f"Missing required service: {svc}"

    def test_ray_head_ipc(self, config: dict | None) -> None:
        """Ray requires ipc=host for shared-memory multiprocessing."""
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        svc = config.get("services", {}).get("ray-head", {})
        assert svc.get("ipc") == "host", "ray-head needs ipc: host"

    def test_ray_head_shm_size(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("docker-compose.yml not created yet")
        svc = config.get("services", {}).get("ray-head", {})
        assert "shm_size" in svc, "ray-head needs shm_size set"


# ── config.yaml (LiteLLM) (§4.3) ───────────────────────────────────────

LITELLM_REQUIRED_KEYS = ["model_list", "general_settings"]


@pytest.mark.config
class TestLiteLLMConfig:
    """config.yaml (LiteLLM) structure per §4.3."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["config_litellm"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet (Phase 2)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        for key in LITELLM_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_model_list_has_entries(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        model_list = config.get("model_list", [])
        assert len(model_list) > 0, "model_list must have at least one entry"

    def test_general_settings_has_master_key(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("config.yaml not created yet")
        gs = config.get("general_settings", {})
        assert "master_key" in gs, "general_settings must declare master_key source"


# ── cluster.yaml (§7.3) ────────────────────────────────────────────────

CLUSTER_REQUIRED_KEYS = ["cluster_name", "provider", "available_node_types"]


@pytest.mark.config
class TestClusterYaml:
    """cluster.yaml structure per §7.3."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["cluster"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("cluster.yaml not created yet (Phase 3)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        for key in CLUSTER_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_head_node_cpu_only(self, config: dict | None) -> None:
        """Head node must be CPU-only per §7.3."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        nodes = config.get("available_node_types", {})
        head = nodes.get("head_node", {})
        instance_type = head.get("node_config", {}).get("InstanceType", "")
        # GPU instance types start with g or p
        assert not instance_type.startswith(("g", "p")), (
            f"Head node should be CPU-only, got {instance_type}"
        )

    # ── New Phase 3 tests ────────────────────────────────────────────────

    def test_docker_image_pinned(self, config: dict | None) -> None:
        """Docker image is pinned to an immutable tag, not :latest (§9.1)."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        docker_cfg = config.get("docker", {})
        image = docker_cfg.get("image", "")
        assert ":latest" not in image, (
            f"cluster.yaml docker.image uses :latest: {image} (§9.1)"
        )
        import re
        assert re.search(r":v?\d+\.\d+\.\d+", image), (
            f"cluster.yaml docker.image not pinned to semver: {image}"
        )

    def test_gpu_worker_min_workers_zero(self, config: dict | None) -> None:
        """GPU worker starts at 0 for scale-to-zero at node level (§13.2)."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        nodes = config.get("available_node_types", {})
        worker = nodes.get("gpu_worker", {})
        assert worker.get("min_workers") == 0, (
            "gpu_worker.min_workers should be 0 (scale-to-zero for nodes)"
        )

    def test_file_mounts_rendered_config(self, config: dict | None) -> None:
        """file_mounts maps rendered_config.yaml for the pre-render workflow."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        mounts = config.get("file_mounts", {})
        assert "/app/rendered_config.yaml" in mounts, (
            "file_mounts must map /app/rendered_config.yaml for pre-render workflow"
        )
        local_path = mounts["/app/rendered_config.yaml"]
        assert local_path.endswith("rendered_config.yaml"), (
            f"file_mounts source should point to rendered_config.yaml, got {local_path}"
        )

    def test_head_start_ray_dashboard_bound(self, config: dict | None) -> None:
        """head_start_ray_commands must bind dashboard to 127.0.0.1 (§9.2)."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        start_cmds = config.get("head_start_ray_commands", [])
        joined = " ".join(start_cmds)
        assert "--dashboard-host=127.0.0.1" in joined, (
            "head_start_ray_commands must include --dashboard-host=127.0.0.1 (§9.2)"
        )

    def test_head_node_no_gpu_resources(self, config: dict | None) -> None:
        """Head node does not declare GPU resources (§7.3)."""
        if config is None:
            pytest.skip("cluster.yaml not created yet")
        nodes = config.get("available_node_types", {})
        head = nodes.get("head_node", {})
        resources = head.get("resources", {})
        assert "GPU" not in resources, (
            "Head node should not declare GPU resources (§7.3)"
        )
        assert "gpu" not in str(resources).lower(), (
            f"Head node resources may contain GPU reference: {resources}"
        )


# ── prometheus.yml (§10.2) ──────────────────────────────────────────────

PROMETHEUS_REQUIRED_KEYS = ["global", "scrape_configs"]

# Expected scrape target addresses from docker-compose service names
EXPECTED_TARGETS = ["ray-head:8080", "litellm:4000"]


@pytest.mark.config
class TestPrometheusConfig:
    """prometheus.yml structure per §10.2."""

    @pytest.fixture
    def config(self, config_files: dict[str, Path]) -> dict | None:
        return _read_yaml(config_files["prometheus"])

    def test_exists(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet (Phase 4)")
        assert isinstance(config, dict)

    def test_has_required_keys(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet")
        for key in PROMETHEUS_REQUIRED_KEYS:
            assert key in config, f"Missing required key: {key}"

    def test_scrape_configs_list(self, config: dict | None) -> None:
        if config is None:
            pytest.skip("prometheus.yml not created yet")
        scrape_configs = config.get("scrape_configs", [])
        assert isinstance(scrape_configs, list)
        assert len(scrape_configs) > 0


# ── .env.example ────────────────────────────────────────────────────────

REQUIRED_ENV_VARS = ["HF_TOKEN", "LITELLM_MASTER_KEY", "MODEL_ID", "MODEL_SOURCE"]


@pytest.mark.config
class TestEnvExample:
    """.env.example documents all required env vars."""

    def test_declares_required_vars(self, config_files: dict[str, Path]) -> None:
        path = config_files["env_example"]
        if not path.exists():
            pytest.skip(".env.example not created yet (Phase 2)")
        content = path.read_text(encoding="utf-8")
        for var in REQUIRED_ENV_VARS:
            assert var in content, f".env.example missing required var: {var}"
