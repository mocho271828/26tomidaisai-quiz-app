from __future__ import annotations

import io
import json
import tarfile
from datetime import timedelta
from hashlib import sha256
from ipaddress import IPv4Address
from pathlib import Path
from unittest.mock import Mock

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtensionOID
from pydantic import SecretStr, ValidationError
from test_load_test_aws import FakeAws, plan
from test_load_test_manifest import CREATED_AT, _manifest

from quiz_app.load_test.controller_contracts import manifest_context
from quiz_app.load_test.db_verifier_queries import APPLICATION_SELECT_COLUMN_ALLOWLIST
from quiz_app.load_test_aws import (
    deployment_api,
    deployment_cli,
    deployment_host,
    deployment_package,
)
from quiz_app.load_test_aws.aws import AwsCallError
from quiz_app.load_test_aws.deployment import (
    archive_files,
    build_runtime_payload,
    create_tls_material,
    human_database_sql,
    new_runtime_secrets,
)
from quiz_app.load_test_aws.deployment_models import (
    DeploymentFile,
    DeploymentSettings,
    HostTopology,
    RuntimeImages,
)

ROOT = Path(__file__).resolve().parents[1]
CA = b"-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----\n"


def settings() -> DeploymentSettings:
    return DeploymentSettings(
        images=RuntimeImages(
            prometheus="docker.io/prom/prometheus@sha256:" + "1" * 64,
            postgres_exporter="quay.io/prometheuscommunity/postgres-exporter@sha256:" + "2" * 64,
            redis_exporter="docker.io/oliver006/redis_exporter@sha256:" + "3" * 64,
            node_exporter="docker.io/prom/node-exporter@sha256:" + "4" * 64,
            toxiproxy="ghcr.io/shopify/toxiproxy@sha256:" + "5" * 64,
        ),
        topology=HostTopology(
            app_hostname="ip-10-77-1-10.ap-northeast-1.compute.internal",
            app_private_ip="10.77.1.10",
            runner_private_ip="10.77.1.11",
            redis_private_ip="10.77.2.12",
            database_endpoint="test.ap-northeast-1.rds.amazonaws.com",
            redis_endpoint="test.apne1.cache.amazonaws.com",
        ),
        postgres_ca_sha256=sha256(CA).hexdigest(),
        node_version="22.19.0",
        node_archive_sha256=sha256(b"node").hexdigest(),
        uv_archive_sha256=sha256(b"uv").hexdigest(),
        docker_package_version="5:28.5.1-1~ubuntu.24.04~noble",
        docker_compose_package_version="2.40.3-1~ubuntu.24.04~noble",
    )


def payload_fixture():
    manifest = _manifest(public_origin="https://10.77.1.10")
    run = plan().model_copy(
        update={
            "run_id": manifest.run_id,
            "schema_revision": manifest.schema_revision,
            "created_at": CREATED_AT,
            "manifest_sha256": manifest_context(manifest).manifest_sha256,
        }
    )
    credentials = new_runtime_secrets(
        SecretStr("A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"), SecretStr("Z9y8X7w6V5u4T3s2R1q0P9n8M7l6K5j4")
    )
    payload = build_runtime_payload(run, manifest, settings(), credentials, CA, ROOT)
    return run, manifest, credentials, payload


def test_tls_certificate_pins_private_ip_and_run_expiry():
    run = plan()
    material = create_tls_material(run, IPv4Address("10.77.1.10"))
    certificate = x509.load_pem_x509_certificate(material.server_certificate.get_secret_value())
    authority = x509.load_pem_x509_certificate(material.ca_certificate.get_secret_value())
    certificate.verify_directly_issued_by(authority)
    assert certificate.not_valid_after_utc == run.expires_at
    names = certificate.extensions.get_extension_for_oid(
        ExtensionOID.SUBJECT_ALTERNATIVE_NAME
    ).value
    assert names.get_values_for_type(x509.IPAddress) == [IPv4Address("10.77.1.10")]
    assert b"PRIVATE KEY" not in material.ca_certificate.get_secret_value()
    assert "PRIVATE KEY" not in repr(material)


def test_payload_separates_host_secrets_and_uses_same_digest_recovery():
    run, manifest, credentials, payload = payload_fixture()
    app = {entry.path: entry.body.get_secret_value() for entry in payload.app}
    runner = {entry.path: entry.body.get_secret_value() for entry in payload.runner}
    all_runner = b"".join(runner.values())
    assert credentials.application_database_password.get_secret_value().encode() not in all_runner
    assert payload.tls.server_key.get_secret_value() not in all_runner
    assert credentials.verifier_database_password.get_secret_value().encode() not in b"".join(
        app.values()
    )
    assert b"image_url" not in app["inputs/manifest.json"]
    release = dict(
        line.split("=", 1) for line in app["release/candidate.env"].decode().splitlines()
    )
    recovery = dict(
        line.split("=", 1) for line in app["rollback/recovery.env"].decode().splitlines()
    )
    assert (
        release["AWS_DEMO_API_IMAGE"]
        == recovery["AWS_DEMO_API_IMAGE"]
        == manifest.artifacts.api_image
    )
    assert release["AWS_DEMO_RELEASE_ID"] != recovery["AWS_DEMO_RELEASE_ID"]
    assert release["AWS_DEMO_RUNTIME_UID"] == "1001"
    assert "@" not in release["AWS_DEMO_REDIS_EXPORTER_ADDR"]
    assert release["AWS_DEMO_REDIS_EXPORTER_USER"] == "metrics"
    assert json.loads(app["proxy/toxiproxy.json"])[0]["listen"] == "172.30.250.2:6379"
    assert json.loads(runner["inputs/orchestrator.json"])["run_id"] == str(run.run_id)


def test_human_sql_has_only_allowlisted_verifier_columns_and_escapes_secrets():
    credentials = new_runtime_secrets(
        SecretStr("A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"), SecretStr("Z9y8X7w6V5u4T3s2R1q0P9n8M7l6K5j4")
    )
    credentials = credentials.model_copy(
        update={"application_database_password": SecretStr("quote'checked")}
    )
    body = human_database_sql(credentials).decode()
    assert "quote''checked" in body
    assert "GRANT SELECT ON ALL TABLES" not in body
    assert "password_hash" not in body and "token_hash" not in body
    assert "REVOKE CREATE, TEMPORARY ON DATABASE quiz FROM PUBLIC" in body
    assert "GRANT pg_read_all_stats TO quiz_verify" in body
    assert "ALTER ROLE" not in body
    for table in {table for table, _ in APPLICATION_SELECT_COLUMN_ALLOWLIST}:
        assert f'ON TABLE public."{table}" TO quiz_verify' in body


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "169.254.169.254", "100.64.0.1", "192.0.2.1", "8.8.8.8"]
)
def test_topology_rejects_non_rfc1918_addresses(address: str):
    with pytest.raises(ValidationError):
        HostTopology.model_validate({**settings().topology.model_dump(), "app_private_ip": address})


def test_image_repository_is_service_specific():
    with pytest.raises(ValidationError):
        RuntimeImages.model_validate(
            {**settings().images.model_dump(), "prometheus": settings().images.toxiproxy}
        )


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "a/../b", ".", "a//b", "a\\b", "a b"])
def test_archive_rejects_unsafe_paths(path: str):
    with pytest.raises(ValidationError):
        DeploymentFile(path=path, body=b"secret")


def test_archive_is_regular_files_only_and_preserves_secret_permissions():
    body = archive_files((DeploymentFile(path="inputs/private.json", body=b"secret"),))
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        member = archive.getmembers()[0]
        assert member.isfile() and member.mode == 0o600 and member.uid == 1001
        assert archive.extractfile(member).read() == b"secret"
    with pytest.raises(ValueError, match="duplicate_path"):
        archive_files(
            (DeploymentFile(path="same", body=b"one"), DeploymentFile(path="same", body=b"two"))
        )


@pytest.mark.parametrize("changed", ["origin", "hash", "ca", "expiry"])
def test_payload_refuses_unbound_plan_before_producing_credentials(changed: str):
    run, manifest, credentials, _ = payload_fixture()
    ca = CA
    if changed == "origin":
        manifest = manifest.model_copy(update={"public_origin": "https://10.77.1.99"})
    elif changed == "hash":
        run = run.model_copy(update={"manifest_sha256": "0" * 64})
    elif changed == "ca":
        ca = b"wrong"
    else:
        run = run.model_copy(update={"created_at": CREATED_AT - timedelta(hours=2)})
    with pytest.raises(ValueError, match="binding_invalid"):
        build_runtime_payload(run, manifest, settings(), credentials, ca, ROOT)


def test_upload_is_encrypted_content_addressed_and_no_overwrite():
    api = FakeAws()
    api.responses[("s3", "put_object")] = {}
    key, digest = deployment_api.upload(api, plan(), "app", b"sensitive", "tar.gz")
    assert key == f"deployment/app/{sha256(b'sensitive').hexdigest()}.tar.gz"
    _, _, request = api.calls[-1]
    assert request["IfNoneMatch"] == "*"
    assert request["ServerSideEncryption"] == "AES256"
    assert digest == sha256(b"sensitive").hexdigest()


def test_existing_object_requires_checksum_readback():
    api = FakeAws()
    api.responses[("s3", "put_object")] = AwsCallError("s3", "put_object", "PreconditionFailed")
    api.responses[("s3", "head_object")] = {"ChecksumSHA256": "wrong", "ContentLength": 3}
    with pytest.raises(ValueError, match="object_mismatch"):
        deployment_api.upload(api, plan(), "app", b"abc", "human.json")


def test_repository_package_refuses_dirty_source(monkeypatch: pytest.MonkeyPatch):
    command = Mock(side_effect=[b"a" * 40 + b"\n", b" M file\n"])
    monkeypatch.setattr(deployment_package, "_run", command)
    with pytest.raises(ValueError, match="clean_source_required"):
        deployment_package.repository_files(ROOT, "a" * 40)
    assert command.call_count == 2


def test_host_bootstrap_script_does_not_apply_schema():
    script = (ROOT / "infra/aws-load-test/deploy-host.sh").read_text()
    assert "alembic upgrade" not in script and "psql" not in script
    assert "useradd --uid 1001" in script
    assert "loginctl enable-linger gsloadtest" in script
    assert "--require-hashes" in script
    assert "filter='data'" in script
    assert "source.extractfile(entry)" in script
    assert "/etc/gs-loadtest/docker-cli/config.json" not in script
    assert "find /etc/gs-loadtest/docker-cli -mindepth 1 -maxdepth 1 -print -quit" in script
    assert "chown -R gsloadtest:gsloadtest /opt/gs-loadtest/repository/load-test/k6/dist" in script


def test_host_command_failure_never_exposes_stdout_secret(monkeypatch: pytest.MonkeyPatch):
    api = FakeAws()
    api.responses[("ssm", "send_command")] = {"Command": {"CommandId": "command"}}
    api.responses[("ssm", "get_command_invocation")] = {
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": "credential should never escape",
    }
    with pytest.raises(ValueError, match="command_failed") as caught:
        deployment_api.command(api, "i-1234567890abcdef0", "true")
    assert "credential" not in str(caught.value)


def test_write_new_never_overwrites_existing_artifact(tmp_path: Path):
    deployment_cli.write_new(tmp_path, "preparation.json", b"first")
    with pytest.raises(FileExistsError):
        deployment_cli.write_new(tmp_path, "preparation.json", b"second")
    assert (tmp_path / "preparation.json").read_bytes() == b"first"
    assert (tmp_path / "preparation.json").stat().st_mode & 0o777 == 0o600


def test_schema_security_requires_exact_nonowner_constraints():
    with pytest.raises(ValidationError):
        deployment_host.AppRoleSecurity(
            safe_role=True, owned_relations=1, owned_schemas=0, owns_database=False
        )


def test_generated_runtime_is_accepted_by_application_settings(tmp_path: Path):
    from quiz_app.config import Settings

    _, manifest, _, payload = payload_fixture()
    runtime = next(file for file in payload.app if file.path == "runtime/runtime.env")
    path = tmp_path / "runtime.env"
    path.write_bytes(runtime.body.get_secret_value())
    parsed = Settings(_env_file=path, aws_demo_expected_event_id=manifest.event.id)
    assert parsed.enable_demo_seed is False
    assert parsed.session_cookie_secure is True
    assert parsed.app_version == manifest.artifacts.source_commit


@pytest.mark.parametrize("failure", [ValueError("guard refused"), KeyboardInterrupt()])
def test_install_failure_and_interrupt_always_request_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException
):
    import sys

    cleanup = Mock()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deployment",
            "install",
            "--plan",
            str(tmp_path / "plan"),
            "--output-directory",
            str(tmp_path),
            "--execute",
        ],
    )
    monkeypatch.setattr(deployment_cli, "require_private_directory", lambda *_: None)
    plan_data = plan().model_dump(mode="json")
    plan_data["notification_email"] = "owner@example.net"
    monkeypatch.setattr(deployment_cli, "read_file", lambda *_: json.dumps(plan_data).encode())
    monkeypatch.setattr(deployment_cli, "BotoAwsApi", Mock())
    monkeypatch.setattr(deployment_cli, "deployment_preflight", Mock(side_effect=failure))
    monkeypatch.setattr(deployment_cli, "request_cleanup", cleanup)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            deployment_cli.main()
    else:
        assert deployment_cli.main() == 1
    assert cleanup.call_count == 1
    assert cleanup.call_args.args[1].run_id == plan().run_id


def test_no_preparation_evidence_if_second_host_cannot_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from argparse import Namespace
    from datetime import UTC, datetime

    from quiz_app.load_test_aws.deployment_models import HumanSchemaConfirmation
    from quiz_app.load_test_aws.models import SchemaReceipt
    from quiz_app.load_test_aws.responses import Stack

    run = plan()
    now = datetime.now(UTC)
    run = run.model_copy(update={"created_at": now - timedelta(minutes=5)})
    confirmation = HumanSchemaConfirmation(
        format="load-test-human-schema-confirmation-v1",
        run_id=run.run_id,
        account_id=run.account_id,
        schema_revision=run.schema_revision,
        schema_and_roles_applied_by_human=True,
        confirmed_at=now,
    )
    schema = SchemaReceipt(
        run_id=run.run_id,
        account_id=run.account_id,
        schema_revision=run.schema_revision,
        verified_at=now,
        schema_applied_by_human=True,
        application_role_is_nonowner=True,
        verifier_role_is_read_only=True,
        single_event_count=1,
    )
    stack = Stack(
        StackId="stack",
        StackName="stack",
        StackStatus="CREATE_COMPLETE",
        Outputs=[
            {"OutputKey": "AppInstanceId", "OutputValue": "i-1234567890abcdef0"},
            {"OutputKey": "RunnerInstanceId", "OutputValue": "i-2234567890abcdef0"},
            {"OutputKey": "AppPrivateIp", "OutputValue": "10.77.1.10"},
            {"OutputKey": "RunnerPrivateIp", "OutputValue": "10.77.1.11"},
        ],
    )
    receipt = deployment_cli.DeploymentReceipt(
        run_id=run.run_id,
        manifest_sha256=run.manifest_sha256,
        source_commit=run.source_commit,
        app_sha256="a" * 64,
        runner_sha256="b" * 64,
        ca_sha256="c" * 64,
        settings=settings(),
    )
    monkeypatch.setattr(
        deployment_cli, "read_file", lambda *_: confirmation.model_dump_json().encode()
    )
    monkeypatch.setattr(deployment_cli, "upload", Mock(return_value=("unused", "a" * 64)))
    monkeypatch.setattr(
        deployment_cli,
        "host_action",
        Mock(
            side_effect=[
                "activated",
                schema.model_dump_json(),
                "sealed",
                ValueError("runner seal failed"),
            ]
        ),
    )
    with pytest.raises(ValueError, match="runner seal failed"):
        deployment_cli.activate(
            Namespace(human_confirmation=tmp_path / "human", output_directory=tmp_path),
            run,
            stack,
            Mock(),
            receipt,
        )
    assert not (tmp_path / "preparation.json").exists()
    assert not (tmp_path / "schema.json").exists()


def test_bootstrap_rejects_old_compose_before_producing_payload():
    with pytest.raises(ValidationError, match="compose_version_unsupported"):
        DeploymentSettings.model_validate(
            {
                **settings().model_dump(),
                "docker_compose_package_version": "2.29.0-1~ubuntu.24.04~noble",
            }
        )


def test_weak_remote_redis_secret_is_not_copied_to_payload():
    with pytest.raises(ValidationError, match="redis_password_invalid"):
        new_runtime_secrets(SecretStr("A1b" * 14), SecretStr("A1b2C3d4E5f6G7h8J9k0L1m2N3p4Q5r6"))


def test_generated_candidate_and_recovery_match_fault_host_contract():
    from quiz_app.load_test.fault_host_config import _parse_env_body, _validate_release_values

    _, _, _, payload = payload_fixture()
    for name in ("release/candidate.env", "rollback/recovery.env"):
        body = next(file.body.get_secret_value() for file in payload.app if file.path == name)
        _validate_release_values(_parse_env_body(body))


def test_single_label_host_is_rejected_before_packaging():
    with pytest.raises(ValidationError, match="remote_dns_name_required"):
        HostTopology.model_validate(
            {**settings().topology.model_dump(), "app_hostname": "gs-loadtest-app"}
        )


@pytest.mark.parametrize(
    "kind,dockerfile", [("app", "backend/Dockerfile"), ("gateway", "frontend/Dockerfile")]
)
def test_oci_build_pins_architecture_and_emits_real_manifest(kind: str, dockerfile: str):
    from quiz_app.load_test_aws.deployment_build_cli import build_arguments

    argv = build_arguments(ROOT, Path("/private/tmp/archive.oci.tar"), "a" * 40, kind)
    assert "--provenance=false" in argv and "--sbom=false" in argv
    assert argv[argv.index("--platform") + 1] == "linux/amd64"
    assert argv[argv.index("--file") + 1] == str(ROOT / dockerfile)
    assert (
        argv[argv.index("--output") + 1]
        == "type=oci,compression=gzip,oci-mediatypes=true,dest=/private/tmp/archive.oci.tar"
    )
    assert "--push" not in argv and "--load" not in argv


def test_oci_output_cannot_inject_exporter_options():
    from quiz_app.load_test_aws.deployment_build_cli import build_arguments

    with pytest.raises(ValueError, match="image_output_invalid"):
        build_arguments(ROOT, Path("/private/tmp/archive,push=true"), "a" * 40, "app")


def test_publish_cli_passes_manifest_binding_and_seals_each_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from argparse import Namespace

    from quiz_app.load_test_aws import deployment_images

    run, manifest, _, _ = payload_fixture()
    inspected = Mock()
    inspect = Mock(return_value=inspected)
    published = deployment_images.PublishedImage(
        image_uri=manifest.artifacts.api_image, manifest_digest="sha256:" + "1" * 64
    )
    publish_image = Mock(return_value=published)
    monkeypatch.setattr(deployment_cli, "load_manifest", lambda *_: manifest)
    monkeypatch.setattr(deployment_images, "inspect_oci_archive", inspect)
    monkeypatch.setattr(deployment_images, "publish_image", publish_image)
    args = Namespace(
        manifest=tmp_path / "manifest.json",
        app_archive=tmp_path / "app.tar",
        gateway_archive=tmp_path / "gateway.tar",
        output_directory=tmp_path,
    )
    api, stack = Mock(), Mock()
    deployment_cli.publish(args, run, stack, api, ROOT)
    assert inspect.call_count == publish_image.call_count == 2
    assert inspect.call_args_list[0].args == (
        args.app_archive,
        run.source_commit,
        "app",
        manifest.artifacts.api_image.rsplit("@", 1)[1],
    )
    assert inspect.call_args_list[1].args[2] == "gateway"
    for call in publish_image.call_args_list:
        assert call.args == (api, run, stack, inspected)
        assert call.kwargs == {"manifest": manifest}
    for kind in ("app", "gateway"):
        assert (
            json.loads((tmp_path / f"published-{kind}.json").read_bytes())["manifest_digest"]
            == published.manifest_digest
        )
