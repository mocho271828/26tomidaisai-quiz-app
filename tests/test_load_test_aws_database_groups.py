from __future__ import annotations

import json
from fnmatch import fnmatchcase

import pytest
from test_load_test_aws import plan

from quiz_app.load_test_aws.bootstrap import execution_policy_documents
from quiz_app.load_test_aws.templates import template_body


@pytest.mark.parametrize(
    ("logical_id", "property_name", "group_name", "service", "resource_type", "action"),
    [
        (
            "Redis",
            "CacheParameterGroupName",
            "default.redis7",
            "elasticache",
            "parametergroup",
            "elasticache:CreateReplicationGroup",
        ),
        (
            "Database",
            "DBParameterGroupName",
            "default.postgres16",
            "rds",
            "pg",
            "rds:CreateDBInstance",
        ),
        (
            "Database",
            "OptionGroupName",
            "default:postgres-16",
            "rds",
            "og",
            "rds:CreateDBInstance",
        ),
    ],
)
def test_database_creation_explicitly_binds_authorized_default_groups(
    logical_id: str,
    property_name: str,
    group_name: str,
    service: str,
    resource_type: str,
    action: str,
) -> None:
    configuration = plan()
    resources = json.loads(template_body("workload"))["Resources"]
    # 暗黙の既定値に頼らず、明示した参照名を固定 ARN の許可と一致させる。
    assert resources[logical_id]["Properties"][property_name] == group_name
    expected_arn = (
        f"arn:aws:{service}:{configuration.region}:{configuration.account_id}:"
        f"{resource_type}:{group_name}"
    )
    permitted = {
        resource
        for body in execution_policy_documents(configuration)
        for statement in json.loads(body)["Statement"]
        if statement["Effect"] == "Allow" and action in statement["Action"]
        for resource in statement["Resource"]
    }
    assert expected_arn in permitted
    unrelated_arn = (
        f"arn:aws:{service}:{configuration.region}:{configuration.account_id}:"
        f"{resource_type}:unrelated-group"
    )
    assert not any(fnmatchcase(unrelated_arn, resource) for resource in permitted)


def test_default_groups_match_pinned_engines_without_creating_shared_resources() -> None:
    resources = json.loads(template_body("workload"))["Resources"]
    redis = resources["Redis"]["Properties"]
    database = resources["Database"]["Properties"]
    assert (redis["Engine"], redis["EngineVersion"]) == ("redis", "7.1")
    assert redis["NumCacheClusters"] == 1
    assert (database["Engine"], database["EngineVersion"]) == ("postgres", "16.15")
    assert redis["CacheSubnetGroupName"] == {"Ref": "RedisSubnets"}
    assert database["DBSubnetGroupName"] == {"Ref": "DatabaseSubnets"}
    assert redis["SecurityGroupIds"] == [{"Ref": "RedisSecurityGroup"}]
    assert database["VPCSecurityGroups"] == [{"Ref": "DatabaseSecurityGroup"}]
    assert redis["UserGroupIds"] == [{"Ref": "RedisUsers"}]
    assert all(
        resource["Type"]
        not in {
            "AWS::ElastiCache::ParameterGroup",
            "AWS::RDS::DBParameterGroup",
            "AWS::RDS::OptionGroup",
        }
        for resource in resources.values()
    )
