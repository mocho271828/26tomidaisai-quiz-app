from __future__ import annotations

from quiz_app.load_test_aws.aws import AwsApi
from quiz_app.load_test_aws.models import PROJECT, CleanupInput, StrictModel
from quiz_app.load_test_aws.responses import Instances, StackResources, StackSummaries, Tag


class ResourceIdentity(StrictModel):
    kind: str
    identifier: str


class RunLedger(StrictModel):
    stack_ids: frozenset[str] = frozenset()
    resources: tuple[ResourceIdentity, ...] = ()

    def identifiers(self, kind: str) -> frozenset[str]:
        return frozenset(item.identifier for item in self.resources if item.kind == kind)

    def permits(self, kind: str, identifier: str, tags: tuple[Tag, ...], run: CleanupInput) -> bool:
        values = {tag.key: tag.value for tag in tags}
        if values.get("Project") != PROJECT or values.get("RunId") != str(run.run_id):
            return False
        if identifier in self.identifiers(kind):
            return True
        if kind != "AWS::EC2::Volume":
            return False
        # AWS予約tagは利用者が任意設定できず、root volume等CFNの間接作成も親stackへ束縛する。
        return (
            values.get("aws:cloudformation:stack-id") in self.stack_ids
            and values.get("aws:cloudformation:stack-name") == run.workload_stack
        )


def read_ledger(api: AwsApi, run: CleanupInput) -> RunLedger:
    """ローカル台帳を失っても削除済みstackを含むCFNのresource履歴を使う。"""
    stack_ids: set[str] = set()
    resources: list[ResourceIdentity] = []
    for page in api.pages("cloudformation", "list_stacks", StackSummaries):
        for stack in page.stacks:
            if stack.name != run.workload_stack:
                continue
            stack_ids.add(stack.stack_id)
            for resource_page in api.pages(
                "cloudformation", "list_stack_resources", StackResources, StackName=stack.stack_id
            ):
                resources.extend(
                    ResourceIdentity(kind=item.kind, identifier=item.physical_id)
                    for item in resource_page.resources
                    if item.physical_id
                )
    instance_ids = sorted(
        {item.identifier for item in resources if item.kind == "AWS::EC2::Instance"}
    )
    if instance_ids:
        # root EBSは独立StackResourceに現れないため、既知EC2の接続IDも物理台帳へ加える。
        for page in api.pages(
            "ec2",
            "describe_instances",
            Instances,
            Filters=[{"Name": "instance-id", "Values": instance_ids}],
        ):
            for reservation in page.reservations:
                for instance in reservation.instances:
                    if instance.identifier not in instance_ids:
                        raise ValueError("aws_load_test_instance_ledger_mismatch")
                    resources.extend(
                        ResourceIdentity(kind="AWS::EC2::Volume", identifier=device.ebs.volume_id)
                        for device in instance.block_devices
                        if device.ebs is not None
                    )
    return RunLedger(stack_ids=frozenset(stack_ids), resources=tuple(resources))


def owned(tags: tuple[Tag, ...], run: CleanupInput) -> bool:
    values = {tag.key: tag.value for tag in tags}
    return values.get("Project") == PROJECT and values.get("RunId") == str(run.run_id)
