from __future__ import annotations

from pydantic import Field

from quiz_app.load_test_aws.aws import ApiModel, AwsApi, EmptyResponse
from quiz_app.load_test_aws.models import CleanupInput, Residual
from quiz_app.load_test_aws.ownership import RunLedger
from quiz_app.load_test_aws.responses import Tag


class Vpc(ApiModel):
    identifier: str = Field(alias="VpcId")


class Vpcs(ApiModel):
    items: tuple[Vpc, ...] = Field(alias="Vpcs")


class Subnet(ApiModel):
    identifier: str = Field(alias="SubnetId")


class Subnets(ApiModel):
    items: tuple[Subnet, ...] = Field(alias="Subnets")


class NetworkInterface(ApiModel):
    tags: tuple[Tag, ...] = Field(default=(), alias="TagSet")
    identifier: str = Field(alias="NetworkInterfaceId")
    state: str = Field(alias="Status")
    managed: bool = Field(alias="RequesterManaged")


class Interfaces(ApiModel):
    items: tuple[NetworkInterface, ...] = Field(alias="NetworkInterfaces")


class SecurityGroup(ApiModel):
    identifier: str = Field(alias="GroupId")
    name: str = Field(alias="GroupName")


class SecurityGroups(ApiModel):
    items: tuple[SecurityGroup, ...] = Field(alias="SecurityGroups")


class RouteAssociation(ApiModel):
    identifier: str = Field(alias="RouteTableAssociationId")
    main: bool = Field(alias="Main")


class RouteTable(ApiModel):
    identifier: str = Field(alias="RouteTableId")
    associations: tuple[RouteAssociation, ...] = Field(alias="Associations")


class Routes(ApiModel):
    items: tuple[RouteTable, ...] = Field(alias="RouteTables")


class Gateway(ApiModel):
    identifier: str = Field(alias="InternetGatewayId")


class Gateways(ApiModel):
    items: tuple[Gateway, ...] = Field(alias="InternetGateways")


def scan_networks(
    api: AwsApi, run: CleanupInput, ledger: RunLedger, *, delete: bool
) -> list[Residual]:
    """CFNが作成したVPC IDを境界に、service管理ENIやdefault networkも検査する。"""
    result: list[Residual] = []
    for vpc in ledger.identifiers("AWS::EC2::VPC"):
        observed = api.call(
            "ec2", "describe_vpcs", Vpcs, Filters=[{"Name": "vpc-id", "Values": [vpc]}]
        )
        if not observed.items:
            continue
        result.append(Residual(kind="vpc", identifier=vpc, state="present"))
        filters = [{"Name": "vpc-id", "Values": [vpc]}]
        children = 0
        for page in api.pages("ec2", "describe_network_interfaces", Interfaces, Filters=filters):
            for item in page.items:
                children += 1
                result.append(
                    Residual(kind="network-interface", identifier=item.identifier, state=item.state)
                )
                if (
                    delete
                    and item.state == "available"
                    and not item.managed
                    and ledger.permits(
                        "AWS::EC2::NetworkInterface", item.identifier, item.tags, run
                    )
                ):
                    api.call(
                        "ec2",
                        "delete_network_interface",
                        EmptyResponse,
                        NetworkInterfaceId=item.identifier,
                    )
        for page in api.pages("ec2", "describe_subnets", Subnets, Filters=filters):
            for item in page.items:
                children += 1
                result.append(Residual(kind="subnet", identifier=item.identifier, state="present"))
                if delete and item.identifier in ledger.identifiers("AWS::EC2::Subnet"):
                    api.call("ec2", "delete_subnet", EmptyResponse, SubnetId=item.identifier)
        for page in api.pages("ec2", "describe_security_groups", SecurityGroups, Filters=filters):
            for item in page.items:
                if item.name == "default":
                    continue
                children += 1
                result.append(
                    Residual(kind="security-group", identifier=item.identifier, state="present")
                )
                if delete and item.identifier in ledger.identifiers("AWS::EC2::SecurityGroup"):
                    api.call("ec2", "delete_security_group", EmptyResponse, GroupId=item.identifier)
        for page in api.pages("ec2", "describe_route_tables", Routes, Filters=filters):
            for item in page.items:
                if any(association.main for association in item.associations):
                    continue
                children += 1
                result.append(
                    Residual(kind="route-table", identifier=item.identifier, state="present")
                )
                if delete and item.identifier in ledger.identifiers("AWS::EC2::RouteTable"):
                    for association in item.associations:
                        api.call(
                            "ec2",
                            "disassociate_route_table",
                            EmptyResponse,
                            AssociationId=association.identifier,
                        )
                    api.call(
                        "ec2", "delete_route_table", EmptyResponse, RouteTableId=item.identifier
                    )
        gateways = api.call(
            "ec2",
            "describe_internet_gateways",
            Gateways,
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}],
        )
        for gateway in gateways.items:
            children += 1
            result.append(
                Residual(kind="internet-gateway", identifier=gateway.identifier, state="attached")
            )
            if delete and gateway.identifier in ledger.identifiers("AWS::EC2::InternetGateway"):
                api.call(
                    "ec2",
                    "detach_internet_gateway",
                    EmptyResponse,
                    InternetGatewayId=gateway.identifier,
                    VpcId=vpc,
                )
                api.call(
                    "ec2",
                    "delete_internet_gateway",
                    EmptyResponse,
                    InternetGatewayId=gateway.identifier,
                )
        if delete and not children:
            api.call("ec2", "delete_vpc", EmptyResponse, VpcId=vpc)
    return result
