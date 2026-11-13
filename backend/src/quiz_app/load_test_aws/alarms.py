"""SNS確認・期限readback後にだけ課金Alarmを作り、既存物は上書きしない。"""

from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, EmptyResponse
from quiz_app.load_test_aws.models import AwsRunPlan
from quiz_app.load_test_aws.responses import Tag
from quiz_app.load_test_aws.templates import tags


class Dimension(ApiModel):
    name: str = Field(alias="Name")
    value: str = Field(alias="Value")


class AlarmConfiguration(ApiModel):
    name: str = Field(alias="AlarmName")
    actions_enabled: bool = Field(alias="ActionsEnabled")
    alarm_actions: tuple[str, ...] = Field(alias="AlarmActions")
    ok_actions: tuple[str, ...] = Field(default=(), alias="OKActions")
    insufficient_data_actions: tuple[str, ...] = Field(default=(), alias="InsufficientDataActions")
    namespace: str = Field(alias="Namespace")
    metric: str = Field(alias="MetricName")
    dimensions: tuple[Dimension, ...] = Field(alias="Dimensions")
    statistic: str = Field(alias="Statistic")
    period: int = Field(alias="Period")
    evaluation_periods: int = Field(alias="EvaluationPeriods")
    datapoints_to_alarm: int = Field(alias="DatapointsToAlarm")
    threshold: float = Field(alias="Threshold")
    comparison: str = Field(alias="ComparisonOperator")
    missing_data: str = Field(alias="TreatMissingData")
    unit: str | None = Field(default=None, alias="Unit")


class ObservedAlarm(AlarmConfiguration):
    arn: str = Field(alias="AlarmArn")


class NamedAlarm(ApiModel):
    name: str = Field(alias="AlarmName")


class AlarmResponse(ApiModel):
    alarms: tuple[ObservedAlarm, ...] = Field(default=(), alias="MetricAlarms")
    composites: tuple[NamedAlarm, ...] = Field(default=(), alias="CompositeAlarms")
    logs: tuple[NamedAlarm, ...] = Field(default=(), alias="LogAlarms")
    next_token: str | None = Field(default=None, alias="NextToken")


class AlarmTags(ApiModel):
    tags: tuple[Tag, ...] = Field(alias="Tags")


def expected_alarm(plan: AwsRunPlan, topic_arn: str) -> AlarmConfiguration:
    """固定DLQだけを監視し、通知先も同一runのSNSへ束縛する。"""
    if topic_arn != f"arn:aws:sns:{plan.region}:{plan.account_id}:{plan.prefix}-notifications":
        raise ValueError("aws_load_test_alarm_topic_mismatch")
    name = f"{plan.prefix}-deadline-dlq"
    return AlarmConfiguration(
        AlarmName=name,
        ActionsEnabled=True,
        AlarmActions=(topic_arn,),
        Namespace="AWS/SQS",
        MetricName="ApproximateNumberOfMessagesVisible",
        Dimensions=(Dimension(Name="QueueName", Value=name),),
        Statistic="Maximum",
        Period=60,
        EvaluationPeriods=1,
        DatapointsToAlarm=1,
        Threshold=0,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )


def require_deadline_alarm(
    api: AwsApi, plan: AwsRunPlan, topic_arn: str, *, create_missing: bool = False
) -> None:
    """一意名・設定・所有tagをreadbackし、作成時も最大一回のPUTだけを許す。"""
    expected = expected_alarm(plan, topic_arn)
    observed = api.call(
        "cloudwatch",
        "describe_alarms",
        AlarmResponse,
        AlarmNames=[expected.name],
        AlarmTypes=["MetricAlarm", "CompositeAlarm", "LogAlarm"],
    )
    if observed.composites or observed.logs or observed.next_token or len(observed.alarms) > 1:
        raise ValueError("aws_load_test_alarm_identity_mismatch")
    if not observed.alarms:
        if not create_missing:
            raise ValueError("aws_load_test_alarm_missing")
        # PutMetricAlarmは更新も兼ねるため、既存物には送らず一度だけ作成して再照会する。
        api.call(
            "cloudwatch",
            "put_metric_alarm",
            EmptyResponse,
            **expected.model_dump(by_alias=True, exclude_none=True),
            AlarmDescription="Deadline invocation could not reach the cleanup workflow",
            Tags=tags(plan),
        )
        require_deadline_alarm(api, plan, topic_arn)
        return
    alarm = observed.alarms[0]
    arn = f"arn:aws:cloudwatch:{plan.region}:{plan.account_id}:alarm:{expected.name}"
    if (
        alarm.arn != arn
        or AlarmConfiguration.model_validate(alarm.model_dump(by_alias=True)) != expected
    ):
        raise ValueError("aws_load_test_alarm_configuration_mismatch")
    tagged = api.call("cloudwatch", "list_tags_for_resource", AlarmTags, ResourceARN=arn)
    actual_tags = {tag.key: tag.value for tag in tagged.tags}
    if any(actual_tags.get(tag["Key"]) != tag["Value"] for tag in tags(plan)):
        raise ValueError("aws_load_test_alarm_ownership_mismatch")
