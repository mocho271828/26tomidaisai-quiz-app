from __future__ import annotations

import json
from pathlib import Path

from quiz_app.load_test_aws.models import PROJECT, PURPOSE, AwsRunPlan, deadline_target


def template_root() -> Path:
    root = Path(__file__).resolve().parents[4]
    path = root / "infra" / "aws-load-test"
    if not (path / "workload.json").is_file():
        raise ValueError("aws_load_test_template_source_missing")
    return path


def template_body(name: str) -> str:
    if name not in {"workload", "control"}:
        raise ValueError("aws_load_test_template_name_invalid")
    source = (template_root() / f"{name}.json").read_text(encoding="utf-8")
    # review用indentをAPIのTemplateBody上限へ含めず、同じJSON値を送信する。
    body = json.dumps(json.loads(source), separators=(",", ":"))
    if len(body.encode()) > 51_200:
        raise ValueError("aws_load_test_template_body_too_large")
    return body


def tags(plan: AwsRunPlan) -> list[dict[str, str]]:
    return [
        {"Key": "Project", "Value": PROJECT},
        {"Key": "Purpose", "Value": PURPOSE},
        {"Key": "RunId", "Value": str(plan.run_id)},
        {"Key": "ExpiresAt", "Value": plan.expires_at.isoformat()},
    ]


def parameters(plan: AwsRunPlan, kind: str) -> list[dict[str, str]]:
    values = {
        "Prefix": plan.prefix,
        "RunId": str(plan.run_id),
        "ExpiresAt": plan.expires_at.isoformat(),
    }
    if kind == "workload":
        values.update(
            {
                "AmiId": plan.ami_id,
                "AvailabilityZone": plan.availability_zone,
                "SecondaryAvailabilityZone": plan.secondary_availability_zone,
            }
        )
    elif kind == "control":
        values.update(
            {
                "NotificationEmail": plan.notification_email.get_secret_value(),
                "DeadlineTargetInput": json.dumps(
                    deadline_target(plan.cleanup_input()), separators=(",", ":")
                ),
                "ScheduleAt": plan.expires_at.strftime("at(%Y-%m-%dT%H:%M:%S)"),
            }
        )
    else:
        raise ValueError("aws_load_test_template_name_invalid")
    return [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]
