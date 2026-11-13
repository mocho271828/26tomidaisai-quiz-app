"""手動deployで専用EC2の問題・設定・DBを揃え、確認環境を起動する。"""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from quiz_app.aws_demo_paths import (
    RUNTIME_ENVIRONMENT_FILE_POLICY,
    AwsDemoDeploymentValidationError,
    read_validated_external_file,
    validate_external_regular_file,
)
from quiz_app.bootstrap.demo_seed import DEMO_DISPLAY_NAME_POLICY_VERSION
from quiz_app.contracts import LiveHealthResponse, ReadinessHealthResponse
from quiz_app.dependency_hosts import DependencyHostValidationError
from quiz_app.quiz_configuration import get_quiz_settings
from quiz_app.rehearsal.content import RehearsalContentError, load_rehearsal_content
from quiz_app.rehearsal.initialization import initialize_rehearsal_database
from quiz_app.rehearsal.preparation import (
    RehearsalEventInput,
    RehearsalPreparationRefusedError,
    prepare_rehearsal_event,
)
from quiz_app.schema_revision import SCHEMA_REVISION
from validation.aws_demo_deployment import (
    AwsDemoReleaseManifest,
    validate_postgres_ca_file,
    validate_release_execution_identity,
    validate_round_backup_host_directory,
    validate_runtime_settings,
)
from validation.aws_review.diagnostics import configure_logging, log_failure, progress
from validation.aws_review.host_contracts import HostInput, HostRefusedError, require_ownership
from validation.aws_review.host_data import (
    BACKUP_DIRECTORY,
    CONFIG_DIRECTORY,
    POSTGRES_CA_FILE,
    RELEASE_FILE,
    RUNTIME_FILE,
    load_secrets,
    reset_dedicated_database,
    runtime_settings,
    write_private_file,
    write_runtime_file,
)
from validation.aws_review.participant_ids import prepare_review_participant_ids
from validation.rehearsal import _require_release_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RETIRED_UPDATE_FILES = (
    CONFIG_DIRECTORY / "aws-demo-review-candidate.env",
    CONFIG_DIRECTORY / "aws-demo-review-candidate-runtime.env",
    CONFIG_DIRECTORY / "aws-demo-review-previous.env",
    CONFIG_DIRECTORY / "aws-demo-review-previous-runtime.env",
)


def _command(
    arguments: list[str],
    *,
    private_ip: str | None = None,
    timeout: int = 300,
) -> str:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "C.UTF-8",
        "PYTHONPATH": str(REPOSITORY_ROOT),
    }
    if private_ip is not None:
        environment["REHEARSAL_PRIVATE_IPV4"] = private_ip
    # 秘密を含む子process出力をSSMへ流さず、失敗時は呼出元で固定codeに変換する。
    result = subprocess.run(  # noqa: S603
        arguments,
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def _compose(
    inputs: HostInput,
    arguments: list[str],
    *,
    timeout: int = 300,
    release_file: Path | None = None,
) -> str:
    directory = REPOSITORY_ROOT / "deploy/aws-demo"
    return _command(
        [
            "/usr/bin/docker",
            "compose",
            "--env-file",
            str(release_file or RELEASE_FILE),
            "--file",
            str(directory / "compose.yaml"),
            "--file",
            str(directory / "compose.rehearsal.yaml"),
            "--file",
            str(directory / "compose.rehearsal-public.yaml"),
            "--file",
            str(directory / "compose.rehearsal-monitoring.yaml"),
            "--project-directory",
            str(directory),
            "--project-name",
            "gs-app-tomidaisai-aws-demo",
            *arguments,
        ],
        private_ip=inputs.private_ip,
        timeout=timeout,
    )


def _remove_retired_update_files() -> None:
    """廃止したDB保持updateの資格情報付き候補・rollback設定を残さない。"""
    for path in RETIRED_UPDATE_FILES:
        path.unlink(missing_ok=True)


def _verify_api_readiness(inputs: HostInput) -> None:
    """負荷分散を経由せず、両APIの版とDB・Redisの準備完了を確認する。"""
    for service in ("api", "api-secondary"):
        responses = [
            _compose(
                inputs,
                [
                    "exec",
                    "-T",
                    "gateway",
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "5",
                    f"http://{service}:8000/api/health/{probe}",
                ],
                timeout=15,
            )
            for probe in ("live", "ready")
        ]
        live = LiveHealthResponse.model_validate_json(responses[0])
        ready = ReadinessHealthResponse.model_validate_json(responses[1])
        # readinessのHTTP 200にはRedis縮退も含まれるため、公開前には本文も照合する。
        if live.version != inputs.source_commit or ready != ReadinessHealthResponse(
            status="ready", database="ok", migration="current", redis="ok"
        ):
            raise HostRefusedError("review_api_readiness_failed")


def _release(inputs: HostInput, event: RehearsalEventInput) -> AwsDemoReleaseManifest:
    context = _command(["/usr/bin/docker", "context", "show"], timeout=10)
    endpoint = _command(
        [
            "/usr/bin/docker",
            "context",
            "inspect",
            context,
            "--format",
            "{{.Endpoints.docker.Host}}",
        ],
        timeout=10,
    )
    if endpoint != "unix:///var/run/docker.sock":
        raise HostRefusedError("review_local_docker_required")
    release = AwsDemoReleaseManifest.model_validate(
        {
            "AWS_DEMO_RELEASE_ID": f"review-{event.event_id.hex}",
            "AWS_DEMO_SOURCE_COMMIT": inputs.source_commit,
            "AWS_DEMO_API_IMAGE": inputs.api_image,
            "AWS_DEMO_GATEWAY_IMAGE": inputs.gateway_image,
            "AWS_DEMO_RUNTIME_ENV_FILE": RUNTIME_FILE,
            "AWS_DEMO_POSTGRES_CA_FILE": POSTGRES_CA_FILE,
            "AWS_DEMO_POSTGRES_CA_SHA256": hashlib.sha256(
                POSTGRES_CA_FILE.read_bytes()
            ).hexdigest(),
            "AWS_DEMO_EXECUTION_HOST": _command(["/usr/bin/hostname", "-f"], timeout=10),
            "AWS_DEMO_DOCKER_CONTEXT": context,
            "AWS_DEMO_DOCKER_ENDPOINT": "unix:///var/run/docker.sock",
            "AWS_DEMO_RUNTIME_UID": os.geteuid(),
            "AWS_DEMO_RUNTIME_GID": os.getegid(),
            "AWS_DEMO_EVENT_ID": event.event_id,
            "AWS_DEMO_ROUND_SCOPE": "rehearsal-six-round",
            "AWS_DEMO_ROUND_BACKUP_HOST_DIRECTORY": BACKUP_DIRECTORY,
            "AWS_DEMO_SCHEMA_REVISION": SCHEMA_REVISION,
            "AWS_DEMO_GATEWAY_PORT": 18080,
        }
    )
    validate_release_execution_identity(release)
    validate_postgres_ca_file(release, REPOSITORY_ROOT)
    # 初回のreleaseはこれから作るため、存在を要求せずbackup内への配置だけを拒否する。
    validate_round_backup_host_directory(
        release,
        REPOSITORY_ROOT,
        protected_paths=(RUNTIME_FILE, POSTGRES_CA_FILE),
        optional_protected_paths=(RELEASE_FILE,),
    )
    write_private_file(
        RELEASE_FILE,
        "".join(
            f"{key}={value}\n"
            for key, value in release.model_dump(
                by_alias=True, mode="json", exclude_none=True
            ).items()
        ),
    )
    return release


def deploy_host(inputs: HostInput) -> None:
    """createとupdateの両方で専用DBを初期状態へ戻して配置する。"""
    if REPOSITORY_ROOT != Path("/opt/gs-app-tomidaisai") or (os.geteuid(), os.getegid()) != (
        1000,
        1000,
    ):
        raise HostRefusedError("review_host_execution_context_invalid")
    _require_release_source(REPOSITORY_ROOT, inputs.source_commit)
    progress("host_ownership")
    require_ownership(inputs)
    _remove_retired_update_files()
    content = load_rehearsal_content(REPOSITORY_ROOT / "content/event-questions.json")
    secrets = load_secrets(inputs)
    event = RehearsalEventInput(
        event_id=uuid4(),
        title=content.title,
        slug=f"review-{inputs.demo_id}",
        scoring_policy_version=get_quiz_settings().scoring.version,
        display_name_policy_version=DEMO_DISPLAY_NAME_POLICY_VERSION,
    )
    settings = runtime_settings(inputs, secrets, event.event_id)
    write_runtime_file(settings)
    release = _release(inputs, event)
    validate_runtime_settings(release, REPOSITORY_ROOT)
    write_private_file(
        CONFIG_DIRECTORY / "review-operator-password",
        secrets.initialization.operator_password.get_secret_value() + "\n",
    )
    progress("host_stopping")
    _compose(inputs, ["down", "--remove-orphans", "--timeout", "30"])
    progress("database_reset")
    reset_dedicated_database(secrets)
    initialize_rehearsal_database(
        settings,
        event,
        secrets.initialization,
        repository_root=REPOSITORY_ROOT,
        postgres_ca_file=POSTGRES_CA_FILE,
    )
    prepare_rehearsal_event(settings, event, content, postgres_ca_file=POSTGRES_CA_FILE)
    prepare_review_participant_ids(secrets, event.event_id)
    progress("host_preflight")
    _command(
        [
            "/bin/bash",
            str(REPOSITORY_ROOT / "scripts/aws-demo-preflight.sh"),
            "--release-file",
            str(RELEASE_FILE),
        ],
        timeout=300,
    )
    _compose(inputs, ["config", "--quiet"])
    progress("host_starting")
    _compose(
        inputs, ["up", "-d", "--wait", "--wait-timeout", "180", "api", "api-secondary", "gateway"]
    )
    _verify_api_readiness(inputs)
    progress("host_ready")
    print(
        json.dumps(
            {
                "status": "ready",
                "mode": inputs.deployment_mode,
                "question_count": len(content.selected_questions),
                "source_commit": inputs.source_commit,
            }
        )
    )


def main(argv: list[str] | None = None) -> int:
    """SSMからの実行を排他化し、秘密値を含まない終了結果だけを返す。"""
    parser = argparse.ArgumentParser(description="手動deploy専用のAWS確認環境作成・DB初期化更新")
    parser.add_argument("--release-input", type=Path, required=True)
    arguments = parser.parse_args(argv)
    configure_logging()
    descriptor: int | None = None
    try:
        validated = validate_external_regular_file(
            arguments.release_input, REPOSITORY_ROOT, RUNTIME_ENVIRONMENT_FILE_POLICY
        )
        inputs = HostInput.model_validate_json(
            read_validated_external_file(
                validated, maximum_bytes=16_384, unavailable_code="review_input_unavailable"
            )
        )
        write_private_file(CONFIG_DIRECTORY / "review-host.lock", "")
        descriptor = os.open(
            CONFIG_DIRECTORY / "review-host.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
        ):
            raise HostRefusedError("review_lock_unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy_host(inputs)
    except (
        OSError,
        UnicodeError,
        ValidationError,
        HostRefusedError,
        AwsDemoDeploymentValidationError,
        RehearsalPreparationRefusedError,
        RehearsalContentError,
        DependencyHostValidationError,
        psycopg.Error,
        BotoCoreError,
        ClientError,
        httpx.HTTPError,
        subprocess.SubprocessError,
    ) as error:
        log_failure("review_host_failed", error)
        return 2
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
