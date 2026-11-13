from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, TypeVar

import boto3
import logfire
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from quiz_app.load_test_aws.models import REGION

ResponseT = TypeVar("ResponseT", bound=BaseModel)


class ApiModel(BaseModel):
    """AWS境界では利用fieldだけを型検証し、SDK metadataは捨てる。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class EmptyResponse(ApiModel):
    pass


class CallerIdentity(ApiModel):
    account: str = Field(alias="Account", pattern=r"^[0-9]{12}$")
    arn: str = Field(alias="Arn", min_length=1)


class AwsCallError(RuntimeError):
    def __init__(self, service: str, operation: str, code: str) -> None:
        self.service = service
        self.operation = operation
        self.code = code
        super().__init__(f"aws_load_test_api:{service}:{operation}:{code}")


class AwsApi(Protocol):
    def call(
        self, service: str, operation: str, response: type[ResponseT], **parameters: object
    ) -> ResponseT: ...

    def pages(
        self, service: str, operation: str, response: type[ResponseT], **parameters: object
    ) -> Iterator[ResponseT]: ...


class BotoAwsApi:
    """再試行を有限にし、生AWS応答をこのadapterの外へ渡さない。"""

    def __init__(self, *, profile: str | None = None) -> None:
        self._session = boto3.Session(profile_name=profile, region_name=REGION)
        self._clients: dict[str, object] = {}
        self._config = Config(
            retries={"total_max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=15,
            proxies={},
            ignore_configured_endpoint_urls=True,
        )

    def _client(self, service: str) -> object:
        if service not in self._clients:
            self._clients[service] = self._session.client(
                service,
                region_name="us-east-1" if service in {"pricing", "acm"} else REGION,
                config=self._config,
            )
        return self._clients[service]

    def call(
        self, service: str, operation: str, response: type[ResponseT], **parameters: object
    ) -> ResponseT:
        method = getattr(self._client(service), operation)
        try:
            with logfire.span(
                "AWS load test {service} {operation}", service=service, operation=operation
            ):
                raw = method(**parameters)
        except ClientError as error:
            # SDK例外本文にはsecretやrequest値が含まれ得るため、境界で固定codeだけへ変換する。
            code = str(error.response["Error"]["Code"])
            raise AwsCallError(service, operation, code) from None
        return response.model_validate(raw)

    def pages(
        self, service: str, operation: str, response: type[ResponseT], **parameters: object
    ) -> Iterator[ResponseT]:
        paginator = self._client(service).get_paginator(operation)
        try:
            for raw in paginator.paginate(**parameters):
                yield response.model_validate(raw)
        except ClientError as error:
            code = str(error.response["Error"]["Code"])
            raise AwsCallError(service, operation, code) from None


def require_account(api: AwsApi, expected: str) -> CallerIdentity:
    identity = api.call("sts", "get_caller_identity", CallerIdentity)
    if identity.account != expected:
        raise ValueError("aws_load_test_account_mismatch")
    return identity
