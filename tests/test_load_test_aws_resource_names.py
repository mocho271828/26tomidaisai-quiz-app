from __future__ import annotations

import json
import re
from string import Template
from uuid import UUID

import pytest
from test_load_test_aws import RUN_ID, plan

from quiz_app.load_test_aws.bootstrap import execution_policy_documents
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.templates import template_body


@pytest.mark.parametrize("run_id", [RUN_ID, UUID("a1234567-1234-4234-8234-123456789012")])
def test_redis_acl_ids_preserve_full_run_id_within_service_length_limit(run_id: UUID) -> None:
    configuration = AwsRunPlan.model_validate({**plan().model_dump(), "run_id": run_id})
    resources = json.loads(template_body("workload"))["Resources"]
    identifiers: list[str] = []
    for resource, field, suffix in (
        ("RedisAppUser", "UserId", "a"),
        ("RedisMonitorUser", "UserId", "m"),
        ("RedisUsers", "UserGroupId", "u"),
    ):
        name = resources[resource]["Properties"][field]["Fn::Sub"]
        value = Template(name).substitute(Prefix=configuration.prefix)
        assert value == f"{configuration.prefix}{suffix}"
        assert len(value) < 40
        assert run_id.hex in value
        assert re.fullmatch(r"[a-z][a-z0-9-]*", value)
        identifiers.append(value)
    assert len(set(identifiers)) == 3
    assert resources["RedisAppUser"]["Properties"]["UserName"] == "default"
    assert resources["RedisMonitorUser"]["Properties"]["UserName"] == "metrics"
    assert resources["Redis"]["Properties"]["ReplicationGroupId"] == {"Ref": "Prefix"}
    assert len(configuration.prefix) <= 40

    statement = next(
        item
        for body in execution_policy_documents(configuration)
        for item in json.loads(body)["Statement"]
        if item["Sid"] == "ManageElasticache"
    )
    acl_resources = {
        resource
        for resource in statement["Resource"]
        if ":user:" in resource or ":usergroup:" in resource
    }
    prefix = f"arn:aws:elasticache:{configuration.region}:{configuration.account_id}"
    assert acl_resources == {
        f"{prefix}:user:{identifiers[0]}",
        f"{prefix}:user:{identifiers[1]}",
        f"{prefix}:usergroup:{identifiers[2]}",
    }
