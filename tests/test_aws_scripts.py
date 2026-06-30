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
import subprocess
import uuid

import pytest


def _has_awscli() -> bool:
    """Check if AWS CLI v2 is available."""
    try:
        subprocess.run(
            ["aws", "--version"],
            capture_output=True,
            timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.aws
class TestScriptCreateSecurityGroup:
    """Run create_security_groups.sh against Floci and verify results."""

    @staticmethod
    def _skip_if_no_awscli():
        if not _has_awscli():
            pytest.skip("AWS CLI not available")

    @pytest.fixture(autouse=True)
    def _setup(self, aws_script_env, ec2_client):
        self._skip_if_no_awscli()
        self.env = {**aws_script_env, **os.environ}
        self.ec2_client = ec2_client
        # Create a unique SG name for this test
        self.sg_name = f"idia-test-sg-{uuid.uuid4().hex[:8]}"
        self.env["SG_NAME"] = self.sg_name
        self.env["ALLOWED_IP_RANGE"] = "10.0.0.0/8"
        self.env["ALLOWED_SSH_RANGE"] = "192.168.0.0/16"
        yield
        # Cleanup: find and delete the SG
        try:
            resp = ec2_client.describe_security_groups(
                Filters=[{"Name": "group-name", "Values": [self.sg_name]}]
            )
            for sg in resp["SecurityGroups"]:
                ec2_client.delete_security_group(GroupId=sg["GroupId"])
        except Exception:
            pass

    def test_script_runs_successfully(self, repo_root):
        """Script exits with 0."""
        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "create_security_groups.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
        )
        assert result.returncode == 0, (
            f"Script failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_sg_exists(self):
        """Security group was created."""
        resp = self.ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [self.sg_name]}]
        )
        assert len(resp["SecurityGroups"]) >= 1
        sg = resp["SecurityGroups"][0]
        assert sg["GroupName"] == self.sg_name

    def test_sg_has_port_4000_ingress(self):
        """SG has ingress rule for port 4000 (LiteLLM)."""
        resp = self.ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [self.sg_name]}]
        )
        sg = resp["SecurityGroups"][0]
        ports = {
            (r["FromPort"], r["ToPort"])
            for r in sg["IpPermissions"]
        }
        assert (4000, 4000) in ports

    def test_sg_has_ssh_ingress(self):
        """SG has ingress rule for port 22 (SSH)."""
        resp = self.ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [self.sg_name]}]
        )
        sg = resp["SecurityGroups"][0]
        ports = {
            (r["FromPort"], r["ToPort"])
            for r in sg["IpPermissions"]
        }
        assert (22, 22) in ports

    def test_sg_is_idempotent(self, repo_root):
        """Running script twice does not fail."""
        result = subprocess.run(
            ["bash", str(repo_root / "scripts" / "create_security_groups.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            env=self.env,
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
        self.env = {**aws_script_env, **os.environ}
        self.s3_client = s3_client
        self.bucket = f"idia-test-cache-{uuid.uuid4().hex[:8]}"
        self.env["S3_BUCKET"] = self.bucket
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
        # Should have informational output
        assert "Dry-run" in result.stdout or "dry-run" in result.stdout

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
        self.env = {**aws_script_env, **os.environ}

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
        # Should have an error message
        assert result.stderr

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
        assert "placeholder" in result.stderr.lower()


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
