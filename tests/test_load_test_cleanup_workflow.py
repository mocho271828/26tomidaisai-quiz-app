from __future__ import annotations

import json
from typing import cast

from quiz_app.load_test_aws.templates import template_body

JsonObject = dict[str, object]


def definition() -> JsonObject:
    template = json.loads(template_body("control"))
    return cast(
        JsonObject,
        json.loads(
            template["Resources"]["StateMachine"]["Properties"]["DefinitionString"]["Fn::Sub"][0]
        ),
    )


def states() -> dict[str, JsonObject]:
    return cast(dict[str, JsonObject], definition()["States"])


def test_all_workflow_targets_exist_and_all_states_are_reachable() -> None:
    nodes = states()
    reachable = {"Initialize"}
    pending = ["Initialize"]
    while pending:
        node = nodes[pending.pop()]
        targets = [node[key] for key in ("Next", "Default") if key in node]
        for field in ("Choices", "Catch"):
            targets.extend(branch["Next"] for branch in cast(list[JsonObject], node.get(field, [])))
        for target in targets:
            assert isinstance(target, str) and target in nodes
            if target not in reachable:
                reachable.add(target)
                pending.append(target)
    assert reachable == set(nodes)


def test_every_api_failure_is_bounded_and_reaches_notification() -> None:
    for name, node in states().items():
        if node["Type"] != "Task":
            continue
        assert 1 <= cast(int, node["TimeoutSeconds"]) <= 130
        catches = cast(list[JsonObject], node["Catch"])
        assert catches[-1]["ErrorEquals"] == ["States.ALL"]
        if name.startswith("Notify"):
            assert cast(str, catches[-1]["Next"]).startswith(("Remember", "Notification"))
        else:
            assert catches[-1]["Next"] == "GuardNotifyRetry"
            # TaskFailed全体のbackoffはAccessDeniedの通知を遅らせるため禁止する。
            retry_errors = cast(list[JsonObject], node["Retry"])[0]["ErrorEquals"]
            assert "States.TaskFailed" not in cast(list[str], retry_errors)
            assert "States.ALL" not in cast(list[str], retry_errors)


def test_final_notification_has_time_reserved_before_hard_timeout() -> None:
    document = definition()
    nodes = states()
    assert document["TimeoutSeconds"] == 86_400
    for name, node in nodes.items():
        if node["Type"] != "Task" or name == "NotifyFailure":
            continue
        guard = nodes[f"Guard{name}"]
        condition = cast(list[JsonObject], guard["Choices"])[0]
        assert condition["Next"] == "NotifyFailure"
        assert condition["Condition"] == (
            "{% $millis() - $toMillis($states.context.Execution.StartTime) >= 85800000 %}"
        )
        assert guard["Default"] == name
    assert nodes["NotifyFailure"]["Next"] == "Failure"
    assert nodes["NotificationFailed"]["Type"] == "Fail"


def test_one_hour_warning_is_recorded_once_and_polling_slows() -> None:
    nodes = states()
    conditions = cast(list[JsonObject], nodes["RetryInterval"]["Choices"])
    assert conditions[1] == {
        "Condition": (
            "{% $millis() - $toMillis($states.context.Execution.StartTime) >= 3600000 "
            "and ($not($exists($states.input.hour_notified)) "
            "or $not($states.input.hour_notified)) %}"
        ),
        "Next": "GuardNotifyHour",
    }
    assert nodes["Wait"]["Seconds"] == 60
    assert nodes["SlowWait"]["Seconds"] == 300
    assert nodes["MarkHourNotified"]["ResultPath"] == "$.hour_notified"
    assert nodes["MarkHourNotified"]["Result"] is True
    assert nodes["RememberHourNotificationFailure"]["Next"] == "MarkHourNotified"
    assert "$merge([$states.input" in cast(str, nodes["Increment"]["Output"])


def test_control_cleanup_retries_preserve_verified_workload_and_notification_failures() -> None:
    nodes = states()
    assert nodes["MarkWorkloadClean"]["ResultPath"] == "$.workload_clean"
    assert nodes["MarkWorkloadClean"]["Result"] is True
    assert cast(list[JsonObject], nodes["ResumePhase"]["Choices"])[-1] == {
        "Variable": "$.workload_clean",
        "BooleanEquals": True,
        "Next": "GuardDeleteDeadline",
    }
    assert nodes["RememberNotificationFailure"]["ResultPath"] == "$.notification_failed"
    assert cast(list[JsonObject], nodes["NotificationStatus"]["Choices"])[0]["Next"] == (
        "NotificationFailed"
    )
    assert nodes["FinalizationResidual"]["Next"] == "GuardNotifyRetry"


def test_absent_worker_requires_paginated_stack_history_and_sealed_observation() -> None:
    nodes = states()
    assert nodes["UnstartedStack"]["Resource"].endswith(":cloudformation:listStacks")
    assert nodes["UnstartedStackNext"]["Parameters"] == {
        "NextToken.$": "$.workload_stacks.NextToken"
    }
    choices = cast(list[JsonObject], nodes["CheckUnstartedStack"]["Choices"])
    assert "StackName = '${Prefix}-workload'" in cast(str, choices[0]["Condition"])
    assert choices[0]["Next"] == "MissingWorker"
    assert choices[1]["Next"] == "GuardUnstartedStackNext"
    assert nodes["EmptyWorkload"]["ResultPath"] == "$.observation"
    assert nodes["EmptyWorkload"]["Next"] == "MarkWorkloadClean"


def test_all_control_deletions_require_service_readback_before_success() -> None:
    nodes = states()
    for deletion, verification in (
        ("DeleteDeadline", "VerifyDeadline"),
        ("DeleteWorker", "VerifyWorkerAbsent"),
        ("DeleteAlarm", "VerifyAlarm"),
        ("DeleteLogs", "VerifyLogs"),
    ):
        assert nodes[deletion]["Next"] == f"Guard{verification}"
    assert nodes["DeleteQueue"]["Next"] == "DrainQueue"
    assert nodes["DrainQueue"]["Next"] == "GuardVerifyQueue"
    assert nodes["DrainWorker"]["Seconds"] == 600
    assert "putFunctionConcurrency" not in json.dumps(nodes)
    assert nodes["LogsAbsent"]["Default"] == "BuildCompletionReport"
    assert nodes["BuildCompletionReport"]["Next"] == "GuardNotifySuccess"
    template = json.loads(template_body("control"))
    resources = template["Resources"]
    assert "logs:CreateLogGroup" not in json.dumps(resources["WorkerRole"])
    assert resources["StateMachine"]["Properties"]["LoggingConfiguration"]["Level"] == "OFF"
    assert len(template_body("control").encode()) <= 51_200


def test_worker_deletion_is_read_back_before_and_after_bounded_drain() -> None:
    nodes = states()
    assert nodes["MarkDeadlineDeleted"]["Next"] == "GuardDeleteWorker"
    assert nodes["DeleteWorker"]["Parameters"] == {"FunctionName": "${WorkerArn}"}
    assert nodes["DrainWorker"]["Next"] == "MarkWorkerDrainElapsed"
    for task, target in (
        ("VerifyWorkerAbsent", "MarkWorkerDeleted"),
        ("VerifyWorkerDrainedAbsent", "MarkWorkerDrained"),
    ):
        assert nodes[task]["Resource"].endswith(":lambda:getFunctionConfiguration")
        assert nodes[task]["Parameters"] == {"FunctionName": "${WorkerArn}"}
        assert nodes[task]["Next"] == "FinalizationResidual"
        assert nodes[task]["Catch"][0] == {
            "ErrorEquals": ["Lambda.ResourceNotFoundException"],
            "ResultPath": None,
            "Next": target,
        }
    assert nodes["MarkWorkerDeleted"]["Next"] == "GuardDrainWorker"
    assert nodes["MarkWorkerDrained"]["Next"] == "GuardDeleteAlarm"
    guard = nodes["GuardDrainWorker"]
    assert guard["Default"] == "DrainWorker"
    assert guard["Choices"] == [
        {
            "Condition": (
                "{% $millis() - $toMillis($states.context.Execution.StartTime) >= 85200000 %}"
            ),
            "Next": "NotifyFailure",
        }
    ]
    assert 86_400 - 85_200 - nodes["DrainWorker"]["Seconds"] == 600
    template = json.loads(template_body("control"))
    statements = template["Resources"]["StateRole"]["Properties"]["Policies"][0]["PolicyDocument"][
        "Statement"
    ]
    assert "lambda:PutFunctionConcurrency" not in json.dumps(statements)
    assert "lambda:GetFunctionConcurrency" not in json.dumps(statements)
    readers = [item for item in statements if "lambda:DeleteFunction" in item["Action"]]
    assert len(readers) == 1
    assert readers[0]["Resource"] == {
        "Fn::Sub": (
            "arn:${AWS::Partition}:lambda:${AWS::Region}:${AWS::AccountId}:function:${Prefix}-cleanup"
        )
    }


def test_control_retries_resume_after_verified_absence_without_replaying_prior_stages() -> None:
    nodes = states()
    choices = cast(list[JsonObject], nodes["ResumePhase"]["Choices"])
    stages = (
        ("MarkDeadlineDeleted", "worker", "GuardDeleteWorker"),
        ("MarkWorkerDeleted", "worker-drain", "GuardDrainWorker"),
        ("MarkWorkerDrainElapsed", "worker-drained", "GuardVerifyWorkerDrainedAbsent"),
        ("MarkWorkerDrained", "alarm", "GuardDeleteAlarm"),
        ("MarkAlarmDeleted", "queue", "GuardDeleteQueue"),
        ("MarkQueueDeleted", "logs", "GuardDeleteLogs"),
    )
    for marker, phase, target in stages:
        assert {
            "And": [
                {"Variable": "$.finalization_phase", "IsPresent": True},
                {"Variable": "$.finalization_phase", "StringEquals": phase},
            ],
            "Next": target,
        } in choices
        assert nodes[marker] == {
            "Type": "Pass",
            "Result": phase,
            "ResultPath": "$.finalization_phase",
            "Next": target,
        }
    assert nodes["VerifyDeadline"]["Catch"][0]["Next"] == "MarkDeadlineDeleted"
    assert nodes["VerifyWorkerAbsent"]["Catch"][0]["Next"] == "MarkWorkerDeleted"
    assert nodes["AlarmsAbsent"]["Default"] == "MarkAlarmDeleted"
    assert nodes["VerifyQueue"]["Catch"][0]["Next"] == "MarkQueueDeleted"
    # 呼び出し側の申告で削除段階を飛ばさず、この実行の不存在確認だけを引き継ぐ。
    assert "finalization_phase" not in json.dumps(nodes["Initialize"])
    assert "$merge([$states.input" in cast(str, nodes["Increment"]["Output"])


def test_alarm_readback_includes_all_service_types_and_only_read_permission_is_broad() -> None:
    nodes = states()
    assert nodes["VerifyAlarm"]["Parameters"] == {
        "AlarmNames": ["${Prefix}-deadline-dlq"],
        "AlarmTypes": ["MetricAlarm", "CompositeAlarm", "LogAlarm"],
    }
    choices = cast(list[JsonObject], nodes["AlarmsAbsent"]["Choices"])
    assert {choice["Variable"] for choice in choices} == {
        "$.alarms.MetricAlarms[0]",
        "$.alarms.CompositeAlarms[0]",
        "$.alarms.LogAlarms[0]",
        "$.alarms.NextToken",
    }
    assert all(choice["Next"] == "FinalizationResidual" for choice in choices)
    statements = json.loads(template_body("control"))["Resources"]["StateRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    read = [s for s in statements if "cloudwatch:DescribeAlarms" in s["Action"]]
    delete = [s for s in statements if "cloudwatch:DeleteAlarms" in s["Action"]]
    assert read == [{"Effect": "Allow", "Action": ["cloudwatch:DescribeAlarms"], "Resource": "*"}]
    assert len(delete) == 1
    assert delete[0]["Resource"] == {
        "Fn::Sub": (
            "arn:aws:cloudwatch:${AWS::Region}:${AWS::AccountId}:alarm:${Prefix}-deadline-dlq"
        )
    }
