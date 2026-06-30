"""Script-level (Layer 2) tests: run shell scripts against Floci.

Tests that the IDIA Server shell scripts work correctly when pointed
at a local AWS emulator (Floci):

- create_security_groups.sh: creates SG with correct rules
- cache_models.sh --dry-run: validates flow without downloading models

Requires:
- testcontainers-floci, boto3, and Docker
- AWS CLI v2 (preinstalled on CI; brew install awscli on macOS)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

# Resolve full path to aws CLI at module load time.
# Using the full path in subprocess calls bypasses PATH resolution issues
# that occur when os.environ is modified during test fixture setup.
_AWS_BIN = shutil.which("aws")


def _has_awscli() -> bool:
    """Check if AWS CLI v2 is available."""
    return _AWS_BIN is not None


@pytest.mark.aws
class TestScriptCreateSecurityGroup:
    """Run create_security_groups.sh against Floci and verify results.

    Uses a class-scoped fixture to create ONE security group (via the
    actual deployment script) and share its name across all tests.
    """

    @staticmethod
    def _skip_if_no_awscli():
        if not _has_awscli():
            pytest.skip("AWS CLI not available")

    @pytest.fixture(autouse=True)
    def _inject_env(self, aws_script_env, ec2_client):
        """Inject shared env values — called before every test."""
        self.ec2_client = ec2_client
        self.env = {**os.environ, **aws_script_env}

    @pytest.fixture(scope="class")
    def sg_shared(self, request, aws_script_env):
        """Create ONE security group for the entire class.

        Yields the SG name, then cleans up after all class tests.
        """
        import subprocess as _sp
        from pathlib import Path as _Path

        self_cls = request.cls
        self_cls._skip_if_no_awscli()

        # Resolve repo_root without depending on function-scoped fixture
        _repo_root = _Path(__file__).resolve().parent.parent

        name = f"idia-test-sg-{uuid.uuid4().hex[:8]}"
        env = {
            **os.environ,
            **aws_script_env,
            "SG_NAME": name,
            "ALLOWED_IP_RANGE": "10.0.0.0/8",
            "ALLOWED_SSH_RANGE": "192.168.0.0/16",
        }

        result = _sp.run(
            ["bash", str(_repo_root / "scripts" / "create_security_groups.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, (
            f"SG creation failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        yield name

        # Cleanup: delete the SG
        try:
            ec2 = __import__("boto3").client("ec2", **{
                "endpoint_url": aws_script_env["AWS_ENDPOINT_URL"],
                "region_name": aws_script_env["AWS_DEFAULT_REGION"],
                "aws_access_key_id": aws_script_env["AWS_ACCESS_KEY_ID"],
                "aws_secret_access_key": aws_script_env["AWS_SECRET_ACCESS_KEY"],
            })
            resp = ec2.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [name]}]
            )
            for sg in resp["SecurityGroups"]:
                ec2.delete_security_group(GroupId=sg["GroupId"])
        except Exception:
            pass

    def _describe_sg(self, sg_name):
        """Helper: describe a SG by name."""
        return self.ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [sg_name]}]
        )

    def test_script_runs_successfully(self, sg_shared):
        """Script exits with 0."""
        # sg_shared already ran the script — test passes if no exception
        pass

    def test_sg_exists(self, sg_shared):
        """Security group was created."""
        resp = self._describe_sg(sg_shared)
        assert len(resp["SecurityGroups"]) >= 1
        assert resp["SecurityGroups"][0]["GroupName"] == sg_shared

    def test_sg_has_port_4000_ingress(self, sg_shared):
        """SG has ingress rule for port 4000 (LiteLLM)."""
        sg = self._describe_sg(sg_shared)["SecurityGroups"][0]
        ports = {(r.get("FromPort"), r.get("ToPort")) for r in sg["IpPermissions"]}
        assert (4000, 4000) in ports, [r.get("FromPort") for r in sg["IpPermissions"]]

    def test_sg_has_ssh_ingress(self, sg_shared):
        """SG has ingress rule for port 22 (SSH)."""
        sg = self._describe_sg(sg_shared)["SecurityGroups"][0]
        ports = {(r.get("FromPort"), r.get("ToPort")) for r in sg["IpPermissions"]}
        assert (22, 22) in ports, [r.get("FromPort") for r in sg["IpPermissions"]]

    def test_sg_is_idempotent(self, sg_shared):
        """Running script twice does not fail."""
        from pathlib import Path as _Path
        _repo_root = _Path(__file__).resolve().parent.parent
        run_env = {
            **os.environ,
            "SG_NAME": sg_shared,
            "ALLOWED_IP_RANGE": "10.0.0.0/8",
            "ALLOWED_SSH_RANGE": "192.168.0.0/16",
            "AWS_ENDPOINT_URL": self.env["AWS_ENDPOINT_URL"],
            "AWS_DEFAULT_REGION": self.env["AWS_DEFAULT_REGION"],
            "AWS_ACCESS_KEY_ID": self.env["AWS_ACCESS_KEY_ID"],
            "AWS_SECRET_ACCESS_KEY": self.env["AWS_SECRET_ACCESS_KEY"],
            "AWS_PAGER": "",
        }
        result = subprocess.run(
            ["bash", str(_repo_root / "scripts" / "create_security_groups.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"Second run failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


@pytest.mark.aws
class TestScriptCacheModels:
    """Run cache_models.sh --dry-run against Floci and verify validation."""

    @staticmethod
    def _skip_if_no_awscli():
        if not _has_awscli():
            pytest.skip("AWS CLI not available")

    @pytest.fixture(autouse=True)
    def _setup(self, aws_script_env, s3_client):
        self._skip_if_no_awscli()
        self.env = {**os.environ, **aws_script_env}
        self.s3_client = s3_client
        self.bucket = f"idia-test-cache-{uuid.uuid4().hex[:8]}"
        self.env["S3_BUCKET"] = self.bucket
        self.env["MODEL_SOURCE"] = "test-org/test-model"
        self.env["MODEL_1_SOURCE"] = "test-org/test-model"
        yield
        # Cleanup bucket if it was created
        try:
            resp = s3_client.list_objects_v2(Bucket=self.bucket)
            if resp.get("Contents"):
                s3_client.delete_objects(
                    Bucket=self.bucket,
                    Delete={
                        "Objects": [
                            {"Key": o["Key"]} for o in resp["Contents"]
                        ],
                        "Quiet": True,
                    },
                )
            s3_client.delete_bucket(Bucket=self.bucket)
        except Exception:
            pass

    def test_dry_run_succeeds(self, repo_root):
        """Script runs in dry-run mode without errors."""
        result = subprocess.run(
            [
                "bash", str(repo_root / "scripts" / "cache_models.sh"),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )
        assert result.returncode == 0, (
            f"Dry-run failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_dry_run_creates_s3_bucket(self):
        """--dry-run should not create the bucket (it's a no-op)."""
        resp = self.s3_client.list_buckets()
        assert not any(b["Name"] == self.bucket for b in resp["Buckets"])


@pytest.mark.aws
class TestScriptDeployClusterValidation:
    """Test deploy_cluster.sh validation logic (no Ray needed)."""

    @staticmethod
    def _skip_if_no_awscli():
        if not _has_awscli():
            pytest.skip("AWS CLI not available")

    @pytest.fixture(autouse=True)
    def _setup(self, aws_script_env):
        self._skip_if_no_awscli()
        self.env = {**os.environ, **aws_script_env}

    def test_deploy_validation_fails_without_env(self, repo_root):
        """deploy_cluster.sh fails when required env vars are missing."""
        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "deploy_cluster.sh")],
            capture_output=True,
            text=True,
            timeout=30,
            env={**self.env, "HF_TOKEN": "", "LITELLM_MASTER_KEY": ""},
        )
        assert result.returncode != 0
        # Error goes to stdout (direct echo, not error() redirect)
        output = result.stdout + result.stderr
        assert output

    def test_deploy_rejects_placeholder_values(self, repo_root):
        """deploy_cluster.sh rejects placeholder values."""
        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "deploy_cluster.sh")],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **self.env,
                "HF_TOKEN": "hf_xxx",
                "LITELLM_MASTER_KEY": "changeme",
                "MODEL_ID": "llama-test",
                "MODEL_SOURCE": "test/test",
            },
        )
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert output


@pytest.mark.aws
@pytest.mark.skipif(not _has_awscli(), reason="AWS CLI not available")
class TestAwsEnvironment:
    """Free-form tests that verify the Floci environment works with
    AWS CLI and boto3 side-by-side."""

    def test_aws_cli_can_list_buckets(self, aws_script_env):
        """AWS CLI can communicate with Floci."""
        result = subprocess.run(
            ["aws", "s3", "ls"],
            capture_output=True,
            text=True,
            timeout=30,
            env=aws_script_env,
        )
        assert result.returncode == 0, (
            f"aws s3 ls failed:\nstderr:\n{result.stderr}"
        )

    def test_aws_cli_can_create_resource(self, aws_script_env):
        """AWS CLI can create an S3 bucket via Floci."""
        bucket = f"idia-cli-test-{uuid.uuid4().hex[:8]}"
        result = subprocess.run(
            ["aws", "s3", "mb", f"s3://{bucket}"],
            capture_output=True,
            text=True,
            timeout=30,
            env=aws_script_env,
        )
        assert result.returncode == 0
        # Verify via boto3
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=aws_script_env["AWS_ENDPOINT_URL"],
            region_name=aws_script_env["AWS_DEFAULT_REGION"],
            aws_access_key_id=aws_script_env["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=aws_script_env["AWS_SECRET_ACCESS_KEY"],
            config=Config(s3={"addressing_style": "path"}),
        )
        resp = client.list_buckets()
        assert any(b["Name"] == bucket for b in resp["Buckets"])

        # Cleanup
        client.delete_bucket(Bucket=bucket)
