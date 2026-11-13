"""GitHub Actionsの手動操作を、設定検証と秘匿ログを通して実行する。"""

import argparse
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import logfire
from botocore.exceptions import BotoCoreError
from pydantic import ValidationError

from quiz_app.load_test_aws.aws import AwsCallError, BotoAwsApi
from validation.aws_review.configuration_errors import configuration_issues
from validation.aws_review.diagnostics import configure_logging, log_failure
from validation.aws_review.models import DeploymentInput, EventStatusInput, ResetInput, ReviewTarget

ROOT = Path(__file__).resolve().parents[2]


def configuration(operation: str) -> ReviewTarget:
    """認証連携後も実行者・sourceを検証し、ローカルからの誤実行を防ぐ。"""
    repository = os.environ.get("AWS_REVIEW_REPOSITORY")
    operator = os.environ.get("AWS_REVIEW_OPERATOR")
    if not repository or not operator:
        raise ValueError("review_manual_owner_execution_required")
    expected = {
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_ACTOR": operator,
        "GITHUB_TRIGGERING_ACTOR": operator,
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise ValueError("review_manual_owner_execution_required")
    values: dict[str, object] = {
        "account_id": os.environ.get("AWS_ACCOUNT_ID", ""),
        "demo_id": os.environ.get("AWS_REVIEW_ID", ""),
    }
    if operation == "destroy":
        return ReviewTarget.model_validate(values)
    if operation == "event-status":
        status = EventStatusInput.model_validate(
            values | {"source_commit": os.environ.get("GITHUB_SHA", "")}
        )
        require_clean_source(status.source_commit)
        return status
    if operation == "reset-event":
        reset = ResetInput.model_validate(
            values
            | {
                "source_commit": os.environ.get("GITHUB_SHA", ""),
                "expected_event_id": os.environ.get("AWS_REVIEW_EXPECTED_EVENT_ID", ""),
                "expected_event_version": os.environ.get("AWS_REVIEW_EXPECTED_EVENT_VERSION", ""),
                "confirmation": os.environ.get("AWS_REVIEW_RESET_CONFIRMATION", ""),
            }
        )
        require_clean_source(reset.source_commit)
        return reset
    values.update(
        {
            "deployment_mode": os.environ.get("AWS_REVIEW_DEPLOYMENT_MODE", "update"),
            # 日時入力を不要にし、create/updateとも監視の有限期限を内部で固定する。
            "monitoring_expires_at": datetime.now(UTC) + timedelta(hours=12),
            "app_instance_type": os.environ.get("AWS_APP_INSTANCE_TYPE", "c7i.xlarge"),
            "public_entrypoint": os.environ.get("AWS_REVIEW_PUBLIC_ENTRYPOINT", "custom-domain"),
            "review_domain_name": os.environ.get("AWS_REVIEW_DOMAIN_NAME", ""),
            "review_certificate_arn": os.environ.get("AWS_REVIEW_CERTIFICATE_ARN", ""),
            "app_az": os.environ.get("AWS_APP_AZ", ""),
            "secondary_az": os.environ.get("AWS_SECONDARY_AZ", ""),
            "ubuntu_ami_id": os.environ.get("AWS_UBUNTU_AMI_ID", ""),
            "database_engine_version": os.environ.get("AWS_DATABASE_ENGINE_VERSION", ""),
            "source_commit": os.environ.get("GITHUB_SHA", ""),
            "vite_event_start_at": os.environ.get("VITE_EVENT_START_AT", ""),
            "vite_survey_url": os.environ.get("VITE_SURVEY_URL", ""),
            "vite_home_url": os.environ.get("VITE_HOME_URL", ""),
        }
    )
    config = DeploymentInput.model_validate(values)
    require_clean_source(config.source_commit)
    return config


def require_clean_source(source_commit: str) -> None:
    """実行対象commitとcheckoutを一致させ、未commitの操作コードを拒否する。"""
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()  # noqa: S603,S607 -- 固定git操作。
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)  # noqa: S603,S607 -- 配置sourceとcommitの一致確認。
    if head != source_commit or dirty.strip():
        raise ValueError("review_source_must_match_clean_commit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="専用AWS確認環境の手動配置・開催確認・初期化・完全撤去"
    )
    parser.add_argument("operation", choices=("deploy", "destroy", "reset-event", "event-status"))
    args = parser.parse_args(argv)
    configure_logging()
    try:
        try:
            target = configuration(args.operation)
        except ValidationError as error:
            log_failure(
                "aws_review_operation_failed",
                error,
                configuration_errors=configuration_issues(error),
            )
            return 1
        api = BotoAwsApi()
        with logfire.span(
            "AWS review {operation}", operation=args.operation, demo_id=target.demo_id
        ):
            if isinstance(target, DeploymentInput):
                from validation.aws_review.deploy import deploy
                from validation.github_json_check import main as validate_json

                if validate_json([]) != 0:
                    raise ValueError("review_json_validation_failed")
                deployment = deploy(api, target, ROOT)
                result = {
                    "status": "deployed",
                    **deployment.model_dump(mode="json"),
                    "operator": "rehearsal-operator",
                    "dns_verified": False,
                }
            elif isinstance(target, ResetInput):
                from validation.aws_review.reset import reset_event

                reset_event(api, target)
                result = {
                    "status": "event_reset",
                    "demo_id": target.demo_id,
                    "event_id": str(target.expected_event_id),
                    "previous_event_version": target.expected_event_version,
                    "source_commit": target.source_commit,
                }
            elif isinstance(target, EventStatusInput):
                from validation.aws_review.event_status import get_event_status, status_summary

                observed = get_event_status(api, target)
                result = {"status": "event_status", **observed.model_dump(mode="json")}
            else:
                from validation.aws_review.destroy import destroy

                destroy(api, target)
                result = {"status": "deleted", "demo_id": target.demo_id}
        print(json.dumps(result))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a") as stream:
                stream.write("\n```json\n" + json.dumps(result, indent=2) + "\n```\n")
                if isinstance(target, DeploymentInput):
                    stream.write(deployment.dns_summary())
                elif isinstance(target, EventStatusInput):
                    stream.write(status_summary(observed, target.source_commit))
        return 0
    except (
        AwsCallError,
        BotoCoreError,
        ValidationError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
    ) as error:
        log_failure("aws_review_operation_failed", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
