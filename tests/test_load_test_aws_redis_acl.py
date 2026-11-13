from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from redis.asyncio.connection import Connection as AsyncConnection
from redis.connection import Connection as SyncConnection
from redis.exceptions import NoPermissionError, ResponseError

from quiz_app.load_test_aws.templates import template_body


@pytest.mark.parametrize(
    ("logical_id", "commands", "channels"),
    [
        (
            "RedisAppUser",
            {
                "+ping",
                "+publish",
                "+subscribe",
                "+unsubscribe",
                "+psubscribe",
                "+punsubscribe",
                "+client|setname",
                "+hello",
                "+select",
            },
            {"&quiz:realtime:*"},
        ),
        (
            "RedisMonitorUser",
            {
                "+ping",
                "+info",
                "+client|setname",
                "+config|get",
                "+slowlog|get",
                "+slowlog|len",
                "+latency|latest",
                "+memory|stats",
                "+cluster|info",
            },
            set(),
        ),
    ],
)
def test_redis_acl_uses_only_reviewed_71_commands(
    logical_id: str, commands: set[str], channels: set[str]
) -> None:
    resources = json.loads(template_body("workload"))["Resources"]
    assert resources["Redis"]["Properties"]["EngineVersion"] == "7.1"
    access = resources[logical_id]["Properties"]["AccessString"].split()
    assert access[:2] == ["on", "-@all"]
    assert set(access[2:]) == commands | channels
    assert "+client|setinfo" not in access
    assert not any(token.startswith(("~", "+@")) for token in access)


@pytest.mark.parametrize("error_type", [ResponseError, NoPermissionError])
def test_sync_redis_handshake_tolerates_unavailable_setinfo(
    error_type: type[ResponseError],
) -> None:
    connection = SyncConnection(username="default", password=uuid4().hex, protocol=2)
    connection._parser = Mock()
    # 実redis-pyの認証後処理を通し、未実装・権限拒否をACL拡張で回避しない。
    replies = [b"OK", error_type("SETINFO unavailable"), error_type("SETINFO unavailable")]
    with (
        patch.object(SyncConnection, "send_command") as send,
        patch.object(SyncConnection, "read_response", side_effect=replies) as read,
    ):
        connection.on_connect_check_health(check_health=False)
    assert [call.args[:2] for call in send.call_args_list] == [
        ("AUTH", "default"),
        ("CLIENT", "SETINFO"),
        ("CLIENT", "SETINFO"),
    ]
    assert read.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ResponseError, NoPermissionError])
async def test_async_redis_handshake_tolerates_unavailable_setinfo(
    error_type: type[ResponseError],
) -> None:
    connection = AsyncConnection(username="default", password=uuid4().hex, protocol=2)
    connection._parser = Mock()
    replies = [b"OK", error_type("SETINFO unavailable"), error_type("SETINFO unavailable")]
    with (
        patch.object(AsyncConnection, "send_command", new_callable=AsyncMock) as send,
        patch.object(AsyncConnection, "read_response", new_callable=AsyncMock) as read,
    ):
        read.side_effect = replies
        await connection.on_connect_check_health(check_health=False)
    assert [call.args[:2] for call in send.call_args_list] == [
        ("AUTH", "default"),
        ("CLIENT", "SETINFO"),
        ("CLIENT", "SETINFO"),
    ]
    assert read.await_count == 3
