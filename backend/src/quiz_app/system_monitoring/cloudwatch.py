"""AWS Dashboard用の固定メトリクスを送信するclient。"""

from typing import Literal, Protocol, cast

import boto3
from botocore.config import Config


class CloudWatchClient(Protocol):
    def put_metric_data(self, **kwargs: object) -> object: ...


def create_client(region: Literal["ap-northeast-1"]) -> CloudWatchClient:
    """hostのIAM roleを使い、任意endpoint/proxyと長いSDK retryを避ける。"""
    return cast(
        CloudWatchClient,
        boto3.client(
            "cloudwatch",
            region_name=region,
            config=Config(
                connect_timeout=2,
                read_timeout=3,
                retries={"total_max_attempts": 2, "mode": "standard"},
                max_pool_connections=2,
                ignore_configured_endpoint_urls=True,
                proxies={},
            ),
        ),
    )
