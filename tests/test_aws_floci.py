"""Service-level (Layer 1) AWS tests using Floci emulator.

Tests S3, EC2, IAM, and STS operations that the IDIA Server
deployment scripts rely on. Every test creates unique resource names
(UUID-based) so they are safe to run in parallel or repeatedly.

Requires:
- testcontainers-floci >= 0.1.0
- boto3 >= 1.35
- Docker daemon running
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────


def _random_name(prefix: str = "idia-test") -> str:
    """Generate a unique resource name for test isolation."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ═══════════════════════════════════════════════════════════════════════════
# S3 — Model cache workflow
# ═══════════════════════════════════════════════════════════════════════════


class TestS3Floci:
    """S3 operations that simulate cache_models.sh."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, s3_client: Any) -> None:
        """Track and clean up buckets after each test."""
        self._buckets: list[str] = []
        yield
        for bucket in self._buckets:
            try:
                # Empty bucket before deletion
                paginator = s3_client.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket):
                    objects = [
                        {"Key": obj["Key"]}
                        for obj in page.get("Contents", [])
                    ]
                    if objects:
                        s3_client.delete_objects(
                            Bucket=bucket,
                            Delete={"Objects": objects, "Quiet": True},
                        )
                s3_client.delete_bucket(Bucket=bucket)
            except Exception:
                log.warning(
                    "Could not clean up bucket %s", bucket, exc_info=True
                )

    # ── Bucket CRUD ────────────────────────────────────────────

    def test_create_bucket(self, s3_client: Any) -> None:
        """Create a bucket and verify it is listed."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)

        resp = s3_client.list_buckets()
        names = [b["Name"] for b in resp["Buckets"]]
        assert name in names

    def test_create_bucket_location(self, s3_client: Any) -> None:
        """CreateBucket returns Location; GetBucketLocation works."""
        name = _random_name()
        result = s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)
        assert "Location" in result
        loc = s3_client.get_bucket_location(Bucket=name)
        assert loc.get("LocationConstraint") in (None, "us-east-1")

    def test_head_bucket(self, s3_client: Any) -> None:
        """HeadBucket returns 200 for existing bucket."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)
        resp = s3_client.head_bucket(Bucket=name)
        assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200

    # ── Object CRUD ─────────────────────────────────────────────

    @pytest.fixture
    def bucket_name(self, s3_client: Any) -> str:
        """Create a reusable bucket for object tests."""
        name = _random_name("idia-obj")
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)
        return name

    def test_upload_and_download(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Upload bytes, download, and verify content integrity."""
        key = "test/model.bin"
        body = b"idia-server-cache-test\x00\xff"
        s3_client.put_object(Bucket=bucket_name, Key=key, Body=body)
        resp = s3_client.get_object(Bucket=bucket_name, Key=key)
        assert resp["Body"].read() == body

    def test_upload_with_metadata(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Upload with custom metadata and verify it is preserved."""
        key = "models/llama/config.json"
        body = b'{"model": "llama-3.1-8b"}'
        s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body,
            Metadata={"model-id": "llama-3.1-8b"},
        )
        resp = s3_client.head_object(Bucket=bucket_name, Key=key)
        assert resp["Metadata"].get("model-id") == "llama-3.1-8b"

    def test_upload_file_and_verify(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Upload via upload_file (file path API) and verify."""
        import tempfile

        key = "uploaded/model.bin"
        with tempfile.NamedTemporaryFile() as tmp:
            tmp.write(b"\x00\x01\x02" * 100)
            tmp.flush()
            s3_client.upload_file(
                Filename=tmp.name, Bucket=bucket_name, Key=key
            )

        resp = s3_client.get_object(Bucket=bucket_name, Key=key)
        assert len(resp["Body"].read()) == 300

    def test_list_objects_prefix(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Upload objects with varied prefixes and filter by prefix."""
        keys = [
            "models/llama/model.bin",
            "models/llama/tokenizer.json",
            "models/qwen/config.json",
            "logs/deploy.log",
        ]
        for k in keys:
            s3_client.put_object(Bucket=bucket_name, Key=k, Body=b"data")

        resp = s3_client.list_objects_v2(
            Bucket=bucket_name, Prefix="models/llama/"
        )
        listed = [o["Key"] for o in resp.get("Contents", [])]
        assert "models/llama/model.bin" in listed
        assert "models/llama/tokenizer.json" in listed
        assert "models/qwen/config.json" not in listed

    def test_list_all_objects(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """List all objects in a bucket."""
        keys = [f"file/{i}.bin" for i in range(5)]
        for k in keys:
            s3_client.put_object(Bucket=bucket_name, Key=k, Body=b"x")
        resp = s3_client.list_objects_v2(Bucket=bucket_name)
        assert len(resp.get("Contents", [])) == 5

    def test_delete_object(self, s3_client: Any, bucket_name: str) -> None:
        """Delete an object and confirm it is gone."""
        key = "temp/test.bin"
        s3_client.put_object(Bucket=bucket_name, Key=key, Body=b"x")
        s3_client.delete_object(Bucket=bucket_name, Key=key)
        resp = s3_client.list_objects_v2(
            Bucket=bucket_name, Prefix="temp/"
        )
        assert not resp.get("Contents")

    def test_delete_objects_batch(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Delete multiple objects in a single DeleteObjects call."""
        keys = [f"batch/{i}.bin" for i in range(5)]
        for k in keys:
            s3_client.put_object(Bucket=bucket_name, Key=k, Body=b"x")
        s3_client.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
        )
        resp = s3_client.list_objects_v2(
            Bucket=bucket_name, Prefix="batch/"
        )
        assert not resp.get("Contents")

    # ── Multipart upload ────────────────────────────────────────

    def test_multipart_upload(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Complete a multipart upload and verify composed content."""
        key = "large-model.bin"
        upload = s3_client.create_multipart_upload(
            Bucket=bucket_name, Key=key
        )
        upload_id = upload["UploadId"]
        parts = []
        for i, data in enumerate([b"part1\n", b"part2\n", b"part3\n"]):
            part = s3_client.upload_part(
                Bucket=bucket_name,
                Key=key,
                PartNumber=i + 1,
                UploadId=upload_id,
                Body=data,
            )
            parts.append({"PartNumber": i + 1, "ETag": part["ETag"]})

        s3_client.complete_multipart_upload(
            Bucket=bucket_name,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        resp = s3_client.get_object(Bucket=bucket_name, Key=key)
        assert resp["Body"].read() == b"part1\npart2\npart3\n"

    def test_abort_multipart_upload(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Abort a multipart upload and verify it is removed."""
        key = "aborted-model.bin"
        upload = s3_client.create_multipart_upload(
            Bucket=bucket_name, Key=key
        )
        upload_id = upload["UploadId"]
        s3_client.abort_multipart_upload(
            Bucket=bucket_name, Key=key, UploadId=upload_id
        )
        list_resp = s3_client.list_multipart_uploads(Bucket=bucket_name)
        uploads = list_resp.get("Uploads", [])
        assert all(u["UploadId"] != upload_id for u in uploads)

    # ── Tagging ─────────────────────────────────────────────────

    def test_bucket_tagging(self, s3_client: Any) -> None:
        """Put and get bucket-level tags."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)
        s3_client.put_bucket_tagging(
            Bucket=name,
            Tagging={"TagSet": [{"Key": "Env", "Value": "test"}]},
        )
        resp = s3_client.get_bucket_tagging(Bucket=name)
        tags = {t["Key"]: t["Value"] for t in resp["TagSet"]}
        assert tags["Env"] == "test"

    def test_object_tagging(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Put and get object-level tags."""
        key = "models/tagged.bin"
        s3_client.put_object(Bucket=bucket_name, Key=key, Body=b"x")
        s3_client.put_object_tagging(
            Bucket=bucket_name,
            Key=key,
            Tagging={"TagSet": [{"Key": "Model", "Value": "Llama"}]},
        )
        resp = s3_client.get_object_tagging(Bucket=bucket_name, Key=key)
        tags = {t["Key"]: t["Value"] for t in resp["TagSet"]}
        assert tags["Model"] == "Llama"

    def test_delete_object_tagging(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Delete object tags."""
        key = "models/untagged.bin"
        s3_client.put_object(Bucket=bucket_name, Key=key, Body=b"x")
        s3_client.put_object_tagging(
            Bucket=bucket_name,
            Key=key,
            Tagging={"TagSet": [{"Key": "Temp", "Value": "yes"}]},
        )
        s3_client.delete_object_tagging(Bucket=bucket_name, Key=key)
        resp = s3_client.get_object_tagging(Bucket=bucket_name, Key=key)
        assert not resp.get("TagSet")

    # ── Pre-signed URLs ─────────────────────────────────────────

    def test_presigned_url(self, s3_client: Any, bucket_name: str) -> None:
        """Generate a pre-signed GET URL and verify it works."""
        import requests

        key = "shared/test.bin"
        body = b"presigned-test"
        s3_client.put_object(Bucket=bucket_name, Key=key, Body=body)

        url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": key},
            ExpiresIn=300,
        )
        resp = requests.get(url, timeout=10)
        assert resp.status_code == 200
        assert resp.content == body

    def test_presigned_put_url(
        self, s3_client: Any, bucket_name: str
    ) -> None:
        """Generate a pre-signed PUT URL and upload via HTTP."""
        import requests

        key = "shared/upload-test.bin"
        body = b"put-via-presigned"
        url = s3_client.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket_name, "Key": key},
            ExpiresIn=300,
        )
        resp = requests.put(url, data=body, timeout=10)
        assert resp.status_code == 200

        dl = s3_client.get_object(Bucket=bucket_name, Key=key)
        assert dl["Body"].read() == body

    # ── Versioning ──────────────────────────────────────────────

    def test_bucket_versioning(self, s3_client: Any) -> None:
        """Enable and verify bucket versioning."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)

        s3_client.put_bucket_versioning(
            Bucket=name,
            VersioningConfiguration={"Status": "Enabled"},
        )
        resp = s3_client.get_bucket_versioning(Bucket=name)
        assert resp["Status"] == "Enabled"

    # ── Full model cache workflow ───────────────────────────────

    def test_model_cache_workflow(self, s3_client: Any) -> None:
        """Simulate cache_models.sh:
        create bucket -> upload directory -> list -> verify -> clean.
        """
        import tempfile
        from pathlib import Path

        bucket = _random_name("idia-cache")
        s3_client.create_bucket(Bucket=bucket)
        self._buckets.append(bucket)

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = Path(tmpdir) / "models--meta-llama--Llama-3.1-8B-Instruct"
            cache.mkdir(parents=True)
            (cache / "model.safetensors").write_bytes(b"x" * 1024)
            (cache / "config.json").write_text('{"model": "llama-3.1-8b"}')

            # Upload the directory tree to S3
            for f in Path(tmpdir).rglob("*"):
                if f.is_file():
                    rel = str(f.relative_to(Path(tmpdir)))
                    s3_client.upload_file(
                        Filename=str(f), Bucket=bucket, Key=rel
                    )

            # Verify objects are in S3
            resp = s3_client.list_objects_v2(Bucket=bucket)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            assert len(keys) == 2
            assert any("model.safetensors" in k for k in keys)
            assert any("config.json" in k for k in keys)

            # Download and verify content
            dl = s3_client.get_object(
                Bucket=bucket,
                Key=[k for k in keys if k.endswith("safetensors")][0],
            )
            assert len(dl["Body"].read()) == 1024

    # ── CORS ────────────────────────────────────────────────────

    def test_bucket_cors(self, s3_client: Any) -> None:
        """Put and get CORS configuration."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)

        cors = {
            "CORSRules": [
                {
                    "AllowedOrigins": ["*"],
                    "AllowedMethods": ["GET", "PUT"],
                    "AllowedHeaders": ["*"],
                }
            ]
        }
        s3_client.put_bucket_cors(Bucket=name, CORSConfiguration=cors)
        resp = s3_client.get_bucket_cors(Bucket=name)
        assert len(resp["CORSRules"]) == 1
        assert resp["CORSRules"][0]["AllowedMethods"] == ["GET", "PUT"]

    # ── Encryption ──────────────────────────────────────────────

    def test_bucket_encryption(self, s3_client: Any) -> None:
        """Put and get default encryption."""
        name = _random_name()
        s3_client.create_bucket(Bucket=name)
        self._buckets.append(name)

        s3_client.put_bucket_encryption(
            Bucket=name,
            ServerSideEncryptionConfiguration={
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "AES256"
                        }
                    }
                ]
            },
        )
        resp = s3_client.get_bucket_encryption(Bucket=name)
        rules = resp["ServerSideEncryptionConfiguration"]["Rules"]
        algo = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        assert algo == "AES256"


# ═══════════════════════════════════════════════════════════════════════════
# EC2 — Security Group workflow
# ═══════════════════════════════════════════════════════════════════════════


class TestEC2Floci:
    """EC2 operations that simulate create_security_groups.sh."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, ec2_client: Any) -> None:
        """Track and clean up security groups after each test."""
        self._sgs: list[str] = []
        yield
        for sg_id in self._sgs:
            try:
                ec2_client.delete_security_group(GroupId=sg_id)
            except Exception:
                log.warning(
                    "Could not clean up SG %s", sg_id, exc_info=True
                )

    def _default_vpc_id(self, ec2_client: Any) -> str:
        resp = ec2_client.describe_vpcs(
            Filters=[{"Name": "isDefault", "Values": ["true"]}]
        )
        assert resp["Vpcs"], "Floci should seed a default VPC"
        return resp["Vpcs"][0]["VpcId"]

    # ── Security Group CRUD ─────────────────────────────────────

    def test_create_security_group(self, ec2_client: Any) -> None:
        """Create a security group and verify properties."""
        vpc_id = self._default_vpc_id(ec2_client)
        name = _random_name("idia-sg")
        resp = ec2_client.create_security_group(
            GroupName=name,
            Description="IDIA test SG",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        g = desc["SecurityGroups"][0]
        assert g["GroupName"] == name
        assert g["VpcId"] == vpc_id

    def test_describe_sg_by_name(self, ec2_client: Any) -> None:
        """Describe a security group by its group-name filter."""
        vpc_id = self._default_vpc_id(ec2_client)
        name = _random_name("idia-sg")
        resp = ec2_client.create_security_group(
            GroupName=name, Description="By name", VpcId=vpc_id
        )
        self._sgs.append(resp["GroupId"])

        desc = ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [name]}]
        )
        assert len(desc["SecurityGroups"]) == 1

    # ── Ingress / Egress ────────────────────────────────────────

    def test_authorize_ingress(self, ec2_client: Any) -> None:
        """Authorize ingress on 4000 (LiteLLM) and 22 (SSH)."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-ingress"),
            Description="Ingress test",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 4000,
                    "ToPort": 4000,
                    "IpRanges": [
                        {
                            "CidrIp": "0.0.0.0/0",
                            "Description": "LiteLLM",
                        }
                    ],
                }
            ],
        )
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [
                        {
                            "CidrIp": "10.0.0.0/8",
                            "Description": "SSH internal",
                        }
                    ],
                }
            ],
        )
        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        ports = {
            (r["FromPort"], r["ToPort"])
            for r in desc["SecurityGroups"][0]["IpPermissions"]
        }
        assert (4000, 4000) in ports
        assert (22, 22) in ports

    def test_authorize_ingress_multi_port(self, ec2_client: Any) -> None:
        """Authorize a range of ports (3000-4000)."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-multi"),
            Description="Multi-port",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 3000,
                    "ToPort": 4000,
                    "IpRanges": [
                        {
                            "CidrIp": "127.0.0.1/32",
                            "Description": "Grafana+LiteLLM",
                        }
                    ],
                }
            ],
        )
        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        multi = [
            r
            for r in desc["SecurityGroups"][0]["IpPermissions"]
            if r["FromPort"] == 3000 and r["ToPort"] == 4000
        ]
        assert len(multi) == 1

    def test_authorize_egress(self, ec2_client: Any) -> None:
        """Authorize egress on port 443 (HTTPS) and verify."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-egress"),
            Description="Egress test",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        ec2_client.authorize_security_group_egress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        # Floci seeds a default allow-all egress rule (no FromPort/ToPort).
        # Our custom rule should be among those that DO have port info.
        egress_rules = desc["SecurityGroups"][0].get("IpPermissionsEgress", [])
        custom = [
            r for r in egress_rules
            if r.get("FromPort") == 443
        ]
        assert len(custom) == 1

    def test_revoke_ingress(self, ec2_client: Any) -> None:
        """Revoke an ingress rule and confirm it is removed."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-revoke"),
            Description="Revoke test",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8080,
                    "ToPort": 8080,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
        ec2_client.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 8080,
                    "ToPort": 8080,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )
        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        assert not any(
            r["FromPort"] == 8080
            for r in desc["SecurityGroups"][0]["IpPermissions"]
        )

    def test_describe_sg_rules(self, ec2_client: Any) -> None:
        """DescribeSecurityGroupRules returns rules."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-rules"),
            Description="Rules test",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 4000,
                    "ToPort": 4000,
                    "IpRanges": [{"CidrIp": "10.0.0.0/8"}],
                }
            ],
        )
        rules = ec2_client.describe_security_group_rules(
            Filters=[{"Name": "group-id", "Values": [sg_id]}]
        )
        assert len(rules["SecurityGroupRules"]) >= 1

    # ── Default resources ───────────────────────────────────────

    def test_default_vpc_exists(self, ec2_client: Any) -> None:
        """Floci seeds a default VPC."""
        vpc_id = self._default_vpc_id(ec2_client)
        assert vpc_id.startswith("vpc-")

    def test_default_security_group_exists(self, ec2_client: Any) -> None:
        """Floci seeds a default security group."""
        resp = ec2_client.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": ["default"]}]
        )
        assert len(resp["SecurityGroups"]) >= 1

    def test_default_subnets_exist(self, ec2_client: Any) -> None:
        """Floci seeds at least one default subnet."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )
        assert len(resp["Subnets"]) >= 1

    # ── Security Group full lifecycle ───────────────────────────

    def test_sg_full_lifecycle(self, ec2_client: Any) -> None:
        """Full SG lifecycle: create -> add rules -> describe -> revoke -> delete."""
        vpc_id = self._default_vpc_id(ec2_client)
        resp = ec2_client.create_security_group(
            GroupName=_random_name("idia-sg-lifecycle"),
            Description="Full lifecycle test",
            VpcId=vpc_id,
        )
        sg_id = resp["GroupId"]
        self._sgs.append(sg_id)

        # Add ingress
        ec2_client.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 4000,
                    "ToPort": 4000,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )

        # Describe
        desc = ec2_client.describe_security_groups(GroupIds=[sg_id])
        assert len(desc["SecurityGroups"]) == 1

        # Add tag
        ec2_client.create_tags(
            Resources=[sg_id],
            Tags=[{"Key": "Environment", "Value": "test"}],
        )
        tagged = ec2_client.describe_security_groups(GroupIds=[sg_id])
        tags = {t["Key"]: t["Value"] for t in tagged["SecurityGroups"][0].get("Tags", [])}
        assert tags.get("Environment") == "test"

        # Revoke
        ec2_client.revoke_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 4000,
                    "ToPort": 4000,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ],
        )

        # Delete (cleanup handles this)
        ec2_client.delete_security_group(GroupId=sg_id)
        self._sgs.remove(sg_id)

        # Verify deletion — Floci returns empty list rather than error
        deleted = ec2_client.describe_security_groups(GroupIds=[sg_id])
        assert len(deleted["SecurityGroups"]) == 0


# ═══════════════════════════════════════════════════════════════════════════
# IAM — Role / Policy / Instance Profile lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestIAMFloci:
    """IAM operations that simulate cluster.yaml permissions."""

    @pytest.fixture(autouse=True)
    def _cleanup(self, iam_client: Any) -> None:
        """Track resources and clean up after each test."""
        self._roles: list[str] = []
        self._policies: list[str] = []
        self._profiles: list[str] = []
        self._users: list[str] = []
        yield
        for p_arn in self._policies:
            try:
                # Detach from all entities first
                for role in self._roles:
                    try:
                        iam_client.detach_role_policy(
                            RoleName=role, PolicyArn=p_arn
                        )
                    except Exception:
                        pass
                iam_client.delete_policy(PolicyArn=p_arn)
            except Exception:
                log.warning("Could not delete policy %s", p_arn)
        for role in self._roles:
            try:
                # Detach all policies
                attached = iam_client.list_attached_role_policies(
                    RoleName=role
                )
                for p in attached.get("AttachedPolicies", []):
                    iam_client.detach_role_policy(
                        RoleName=role, PolicyArn=p["PolicyArn"]
                    )
                iam_client.delete_role(RoleName=role)
            except Exception:
                log.warning("Could not delete role %s", role)
        for profile in self._profiles:
            try:
                ip = iam_client.get_instance_profile(
                    InstanceProfileName=profile
                )
                for role in ip["InstanceProfile"]["Roles"]:
                    iam_client.remove_role_from_instance_profile(
                        InstanceProfileName=profile,
                        RoleName=role["RoleName"],
                    )
                iam_client.delete_instance_profile(
                    InstanceProfileName=profile
                )
            except Exception:
                log.warning(
                    "Could not delete instance profile %s", profile
                )

    # ── Roles ───────────────────────────────────────────────────

    def test_create_role(self, iam_client: Any) -> None:
        """Create an IAM role and verify properties."""
        name = _random_name("idia-role")
        iam_client.create_role(
            RoleName=name,
            AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
        )
        self._roles.append(name)
        resp = iam_client.get_role(RoleName=name)
        assert resp["Role"]["RoleName"] == name

    def test_list_roles(self, iam_client: Any) -> None:
        """List all roles includes user-created ones."""
        name = _random_name("idia-role")
        iam_client.create_role(
            RoleName=name,
            AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
        )
        self._roles.append(name)
        resp = iam_client.list_roles()
        assert any(r["RoleName"] == name for r in resp["Roles"])

    # ── Policies ────────────────────────────────────────────────

    def test_create_policy(self, iam_client: Any) -> None:
        """Create an IAM policy and verify it exists."""
        name = _random_name("idia-policy")
        resp = iam_client.create_policy(
            PolicyName=name,
            PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:ListAllMyBuckets","Resource":"*"}]}',
        )
        arn = resp["Policy"]["Arn"]
        self._policies.append(arn)
        get_resp = iam_client.get_policy(PolicyArn=arn)
        assert get_resp["Policy"]["PolicyName"] == name

    def test_list_policies(self, iam_client: Any) -> None:
        """Created policies appear in list."""
        name = _random_name("idia-policy")
        resp = iam_client.create_policy(
            PolicyName=name,
            PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"ec2:Describe*","Resource":"*"}]}',
        )
        arn = resp["Policy"]["Arn"]
        self._policies.append(arn)
        list_resp = iam_client.list_policies(Scope="Local")
        assert any(p["PolicyName"] == name for p in list_resp["Policies"])

    def test_attach_detach_role_policy(
        self, iam_client: Any
    ) -> None:
        """Attach a policy to a role, then detach it."""
        role_name = _random_name("idia-role")
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
        )
        self._roles.append(role_name)

        policy_name = _random_name("idia-policy")
        policy_resp = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:*","Resource":"*"}]}',
        )
        policy_arn = policy_resp["Policy"]["Arn"]
        self._policies.append(policy_arn)

        iam_client.attach_role_policy(
            RoleName=role_name, PolicyArn=policy_arn
        )
        attached = iam_client.list_attached_role_policies(
            RoleName=role_name
        )
        assert any(p["PolicyArn"] == policy_arn for p in attached["AttachedPolicies"])

        iam_client.detach_role_policy(
            RoleName=role_name, PolicyArn=policy_arn
        )
        attached = iam_client.list_attached_role_policies(
            RoleName=role_name
        )
        assert not any(p["PolicyArn"] == policy_arn for p in attached["AttachedPolicies"])

    # ── Instance Profiles ───────────────────────────────────────

    def test_create_instance_profile(self, iam_client: Any) -> None:
        """Create an instance profile."""
        name = _random_name("idia-profile")
        iam_client.create_instance_profile(InstanceProfileName=name)
        self._profiles.append(name)
        resp = iam_client.get_instance_profile(
            InstanceProfileName=name
        )
        assert resp["InstanceProfile"]["InstanceProfileName"] == name

    def test_add_remove_role_to_instance_profile(
        self, iam_client: Any
    ) -> None:
        """Add a role to an instance profile, then remove it."""
        role_name = _random_name("idia-role")
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument='{"Version":"2012-10-17","Statement":[]}',
        )
        self._roles.append(role_name)

        profile_name = _random_name("idia-profile")
        iam_client.create_instance_profile(
            InstanceProfileName=profile_name
        )
        self._profiles.append(profile_name)

        iam_client.add_role_to_instance_profile(
            InstanceProfileName=profile_name, RoleName=role_name
        )
        resp = iam_client.get_instance_profile(
            InstanceProfileName=profile_name
        )
        role_names = [r["RoleName"] for r in resp["InstanceProfile"]["Roles"]]
        assert role_name in role_names

        iam_client.remove_role_from_instance_profile(
            InstanceProfileName=profile_name, RoleName=role_name
        )
        resp = iam_client.get_instance_profile(
            InstanceProfileName=profile_name
        )
        assert not resp["InstanceProfile"]["Roles"]

    # ── AWS managed policies exist ──────────────────────────────

    def test_aws_managed_policies_seeded(self, iam_client: Any) -> None:
        """Floci seeds common AWS managed policies."""
        resp = iam_client.list_policies(Scope="AWS")
        arns = [p["Arn"] for p in resp["Policies"]]
        assert any("AdministratorAccess" in arn for arn in arns)
        assert any("AmazonEC2FullAccess" in arn for arn in arns)
        assert any("AmazonS3FullAccess" in arn for arn in arns)


# ═══════════════════════════════════════════════════════════════════════════
# STS — Security Token Service
# ═══════════════════════════════════════════════════════════════════════════


class TestSTSFloci:
    """STS credential validation."""

    def test_get_caller_identity(
        self, sts_client: Any
    ) -> None:
        """GetCallerIdentity returns valid credentials."""
        resp = sts_client.get_caller_identity()
        assert "Account" in resp
        assert "UserId" in resp
        assert "Arn" in resp

    def test_assume_role(self, iam_client: Any, sts_client: Any) -> None:
        """AssumeRole returns temporary credentials."""
        role_name = _random_name("idia-assume-role")
        iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=(
                '{"Version":"2012-10-17","Statement":['
                '{"Effect":"Allow","Principal":{"AWS":"*"},"Action":"sts:AssumeRole"}]}'
            ),
        )
        role = iam_client.get_role(RoleName=role_name)
        role_arn = role["Role"]["Arn"]

        # Manually clean up since we don't have auto-clean for this
        try:
            resp = sts_client.assume_role(
                RoleArn=role_arn,
                RoleSessionName="test-session",
                DurationSeconds=900,
            )
            creds = resp["Credentials"]
            assert "AccessKeyId" in creds
            assert "SecretAccessKey" in creds
            assert "SessionToken" in creds
            assert creds["Expiration"] is not None
        finally:
            iam_client.delete_role(RoleName=role_name)
