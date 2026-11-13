"""配置済みhostをEvent確認・初期化前に読み取り検証する。"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from quiz_app.config import Settings
from validation.aws_demo_deployment import (
    AwsDemoReleaseManifest,
    validate_postgres_ca_file,
    validate_release_execution_identity,
    validate_release_manifest_file,
    validate_round_backup_host_directory,
    validate_runtime_settings,
)
from validation.aws_review.host_contracts import HostInput, HostRefusedError
from validation.aws_review.host_data import (
    BACKUP_DIRECTORY,
    POSTGRES_CA_FILE,
    RELEASE_FILE,
    RUNTIME_FILE,
)


@dataclass(frozen=True, slots=True)
class ExistingHostConfiguration:
    release: AwsDemoReleaseManifest
    settings: Settings


def validate_existing_host(
    inputs: HostInput,
    repository_root: Path,
    *,
    execution_host: str,
    docker_context: str,
    docker_endpoint: str,
) -> ExistingHostConfiguration:
    """配置済みの実行先・設定を検証し、fileやDBを書き換えず返す。"""

    release = validate_release_manifest_file(RELEASE_FILE, repository_root)
    if (
        release.round_scope != "rehearsal-six-round"
        or release.runtime_env_file != RUNTIME_FILE
        or release.postgres_ca_file != POSTGRES_CA_FILE
        or release.round_backup_host_directory != BACKUP_DIRECTORY
        or release.release_id != f"review-{release.event_id.hex}"
        or release.execution_host != execution_host
        or release.docker_context != docker_context
        or release.docker_endpoint != docker_endpoint
    ):
        raise HostRefusedError("review_update_release_identity_mismatch")
    for previous, candidate in (
        (release.api_image, inputs.api_image),
        (release.gateway_image, inputs.gateway_image),
    ):
        if previous.partition("@")[0] != candidate.partition("@")[0]:
            raise HostRefusedError("review_update_image_repository_mismatch")
    validate_release_execution_identity(release)
    validate_postgres_ca_file(release, repository_root)
    validate_round_backup_host_directory(
        release,
        repository_root,
        protected_paths=(RELEASE_FILE, RUNTIME_FILE, POSTGRES_CA_FILE),
    )
    settings = validate_runtime_settings(release, repository_root)
    database = make_url(settings.database_url)
    redis = make_url(settings.redis_url)
    if (
        database.host != inputs.rds_hostname
        or database.database != "quiz"
        or database.username != "rehearsal_app"
        or database.port != 5432
        or redis.host != inputs.redis_hostname
        or redis.port != 6379
        or redis.database != "0"
        or [str(origin).rstrip("/") for origin in settings.realtime_allowed_origins]
        != [inputs.public_url]
        or settings.enable_demo_seed
    ):
        raise HostRefusedError("review_update_runtime_target_mismatch")
    return ExistingHostConfiguration(release, settings)
