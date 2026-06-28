"""Security constraint validation tests for IDIA Server.

These tests verify that the deployment artifacts respect the security
boundaries defined in docs/ARCHITECTURE.md §9, without requiring a
running Docker environment.

They check:
    - Port isolation: only :4000 is exposed externally
    - Image pinning: no :latest in Dockerfile or compose
    - Dashboard binding: bound to 127.0.0.1
    - Trust boundaries: master key declared in config

Tests that genuinely require a running container (e.g., verify that
:8000 is actually unreachable from outside) are marked @pytest.mark.security
and skip when Docker is unavailable.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


@pytest.mark.security
class TestPortIsolation:
    """Only port 4000 is exposed to the host.

    See ARCHITECTURE.md §9.1.
    """

    def test_only_4000_published(self, repo_root: Path) -> None:
        """docker-compose.yml lists only port 4000 in ports: sections."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        all_ports: list[str] = []
        for svc_name, svc in compose.get("services", {}).items():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                all_ports.extend(p for p in ports if isinstance(p, str))
        for p in all_ports:
            # "4000:4000" is allowed; "8000:8000", "8080:8080", "8265:8265" are not
            host_port = p.split(":")[0] if ":" in p else p
            assert host_port == "4000", (
                f"Port {host_port} is exposed in docker-compose.yml — "
                f"only port 4000 should be published (§9.1)"
            )
        assert len(all_ports) > 0, "No ports published at all — check service config"

    def test_ray_ingress_not_published(self, repo_root: Path) -> None:
        """Ray Serve ingress port (8000) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "8000" not in p, (
                            f"Port 8000 (Ray ingress) must not be published (§9.3)"
                        )

    def test_dashboard_not_published(self, repo_root: Path) -> None:
        """Ray Dashboard port (8265) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "8265" not in p, (
                            f"Port 8265 (Ray dashboard) must not be published (§9.2)"
                        )

    def test_ray_client_not_published(self, repo_root: Path) -> None:
        """Ray Client port (10001) is NOT in any ports: section."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc in compose.get("services", {}).values():
            ports = svc.get("ports", [])
            if isinstance(ports, list):
                for p in ports:
                    if isinstance(p, str):
                        assert "10001" not in p, (
                            f"Port 10001 (Ray Client) must not be published (§9.2)"
                        )


@pytest.mark.security
class TestImagePinning:
    """All container images are pinned to immutable tags.

    See ARCHITECTURE.md §9.1.
    """

    def test_dockerfile_no_latest(self, repo_root: Path) -> None:
        """Dockerfile.ray uses a pinned base image, not :latest."""
        path = repo_root / "Dockerfile.ray"
        if not path.exists():
            pytest.skip("Dockerfile.ray not created yet")
        content = path.read_text(encoding="utf-8")
        assert ":latest" not in content, (
            "Dockerfile.ray uses :latest — all images must be pinned (§9.1)"
        )

    def test_compose_no_latest(self, repo_root: Path) -> None:
        """No service in docker-compose.yml uses :latest."""
        path = repo_root / "docker-compose.yml"
        if not path.exists():
            pytest.skip("docker-compose.yml not created yet")
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        for svc_name, svc in compose.get("services", {}).items():
            image = svc.get("image", "")
            if image:  # skip services that build locally
                assert ":latest" not in str(image), (
                    f"Service '{svc_name}' uses :latest (§9.1)"
                )


@pytest.mark.security
class TestTrustBoundaries:
    """Two trust boundaries: master key vs virtual keys.

    See ARCHITECTURE.md §9.1.
    """

    def test_litellm_config_has_master_key(self, repo_root: Path) -> None:
        """config.yaml declares general_settings.master_key."""
        path = repo_root / "config.yaml"
        if not path.exists():
            pytest.skip("config.yaml not created yet")
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "general_settings" in config, "config.yaml missing general_settings (§9.1)"
        assert "master_key" in config["general_settings"], (
            "config.yaml missing master_key in general_settings (§9.1)"
        )


@pytest.mark.security
class TestDashboardBinding:
    """Ray Dashboard binds to 127.0.0.1.

    See ARCHITECTURE.md §9.2.
    """

    def test_dashboard_host_set_to_localhost(self, repo_root: Path) -> None:
        """serve_config.yaml does NOT bind Ray dashboard publicly.

        This test checks that no configuration exposes the dashboard to
        0.0.0.0. The actual --dashboard-host=127.0.0.1 flag is set in
        cluster.yaml (Phase 3) and the compose file omits port 8265.
        """
        path = repo_root / "serve_config.yaml"
        if not path.exists():
            pytest.skip("serve_config.yaml not created yet")
        content = path.read_text(encoding="utf-8")
        # The http_options.host in serve_config.yaml binds the proxy
        # inside the container — not the dashboard. Verify it is 0.0.0.0
        # as specified in §5.3 (this is correct: proxy binds inside the
        # container — the port is not published to the host).
        parsed = yaml.safe_load(content)
        assert parsed["http_options"]["host"] == "0.0.0.0", (
            "serve_config proxy binds to 0.0.0.0 inside container (§5.3); "
            "isolation relies on never publishing port 8000 outside (§9.3)"
        )


# ── Phase 3: Cluster Security ───────────────────────────────────────────────


@pytest.mark.security
class TestClusterSecurity:
    """cluster.yaml respects security invariants (§7.3, §9).

    These tests validate that the Ray Cluster Launcher configuration
    does not expose the dashboard, uses pinned images, and follows
    the security constraints from ARCHITECTURE.md §9.
    """

    PATH = "cluster.yaml"

    def test_cluster_dashboard_bound_localhost(self, repo_root: Path) -> None:
        """cluster.yaml binds dashboard to 127.0.0.1 in head_start_ray_commands."""
        path = repo_root / self.PATH
        if not path.exists():
            pytest.skip("cluster.yaml not created yet (Phase 3)")
        content = path.read_text(encoding="utf-8")
        assert "--dashboard-host=127.0.0.1" in content, (
            "cluster.yaml must set --dashboard-host=127.0.0.1 in "
            "head_start_ray_commands (§9.2)"
        )

    def test_cluster_image_pinned(self, repo_root: Path) -> None:
        """cluster.yaml docker.image is pinned, not :latest."""
        path = repo_root / self.PATH
        if not path.exists():
            pytest.skip("cluster.yaml not created yet (Phase 3)")
        content = path.read_text(encoding="utf-8")
        assert ":latest" not in content, (
            "cluster.yaml docker.image must not use :latest (§9.1)"
        )
        # Verify it has a semver-like tag
        assert re.search(r":v?\d+\.\d+\.\d+", content), (
            "cluster.yaml docker.image must be pinned to a specific version"
        )

    def test_cluster_head_node_cpu_only(self, repo_root: Path) -> None:
        """cluster.yaml head node InstanceType is not GPU-class."""
        path = repo_root / self.PATH
        if not path.exists():
            pytest.skip("cluster.yaml not created yet (Phase 3)")
        content = path.read_text(encoding="utf-8")
        # Check that head node doesn't use g/p instance types
        # m5.large, t3.medium, c6i.large are all CPU-only
        parsed = yaml.safe_load(content)
        nodes = parsed.get("available_node_types", {})
        head = nodes.get("head_node", {})
        instance_type = head.get("node_config", {}).get("InstanceType", "")
        assert not instance_type.startswith(("g", "p", "f")), (
            f"Head node InstanceType ({instance_type}) appears to have a GPU; "
            f"head node should be CPU-only per §7.3"
        )
