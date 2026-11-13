"""開催IDリストを準備する人手用SQLをDB接続なしで生成する。"""

import argparse
import os
from itertools import product
from pathlib import Path
from random import SystemRandom
from string import ascii_uppercase
from uuid import UUID

import logfire
from psycopg import sql
from pydantic import BaseModel, ConfigDict, ValidationError

from quiz_app.models.participant_ids import PARTICIPANT_ID_POOL_VERSION
from quiz_app.schema_revision import SCHEMA_REVISION


class PoolSqlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    output: Path


def build_pool_preparation_statement(event_id: UUID) -> sql.Composed:
    """呼出側のtransaction内で、一度だけpoolを準備する共通SQLを作る。"""

    labels = ["".join(characters) for characters in product(ascii_uppercase, repeat=3)]
    SystemRandom().shuffle(labels)
    values = sql.SQL(",\n").join(
        sql.SQL("({}, {})").format(sql.Literal(ordinal), sql.Literal(label))
        for ordinal, label in enumerate(labels, 1)
    )
    sequence_name = f"participant_id_{event_id.hex}"
    # 接続なしの配布SQLでも既存driverのliteral処理を通し、手書きescapeを避ける。
    statement = sql.SQL("""SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $guard$
BEGIN
    IF (SELECT array_agg(version_num::text) FROM public.alembic_version)
       IS DISTINCT FROM ARRAY[{schema_revision}]::text[] THEN
        RAISE EXCEPTION 'participant_id_pool_schema_revision_mismatch';
    END IF;
    PERFORM 1 FROM public.events WHERE id = {event_id} FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'participant_id_pool_event_missing';
    END IF;
    IF EXISTS (SELECT 1 FROM public.participant_id_pools
               WHERE event_id = {event_id})
       OR to_regclass({sequence_regclass}) IS NOT NULL THEN
        RAISE EXCEPTION 'participant_id_pool_already_prepared';
    END IF;
END;
$guard$;
CREATE TEMP TABLE participant_id_candidates (
    shuffle_order integer PRIMARY KEY,
    public_label varchar(3) NOT NULL UNIQUE CHECK (public_label ~ '^[A-Z]{{3}}$')
) ON COMMIT DROP;
INSERT INTO participant_id_candidates (shuffle_order, public_label) VALUES
{values};
CREATE TEMP TABLE participant_id_available ON COMMIT DROP AS
SELECT row_number() OVER (ORDER BY candidate.shuffle_order)::integer AS ordinal,
       candidate.public_label
FROM participant_id_candidates AS candidate
WHERE NOT EXISTS (
    SELECT 1 FROM public.participants AS existing
    WHERE existing.event_id = {event_id}
      AND existing.public_label = candidate.public_label
);
INSERT INTO public.participant_id_pools (event_id, capacity, version)
SELECT {event_id}, count(*)::integer, {version}
FROM participant_id_available;
INSERT INTO public.participant_id_slots (event_id, ordinal, public_label)
SELECT {event_id}, ordinal, public_label FROM participant_id_available;
DO $sequence$
DECLARE
    pool_capacity integer;
BEGIN
    SELECT capacity INTO STRICT pool_capacity FROM public.participant_id_pools
    WHERE event_id = {event_id};
    IF pool_capacity > 0 THEN
        -- UNLOGGED/TEMPORARYを指定しない永続sequenceを作成する。
        EXECUTE format(
            'CREATE SEQUENCE public.%I AS integer MINVALUE 1 MAXVALUE %s '
            'START WITH 1 INCREMENT BY 1 CACHE 1 NO CYCLE',
            {sequence_name}, pool_capacity
        );
    END IF;
END;
$sequence$;
""")
    return statement.format(
        event_id=sql.Literal(event_id),
        version=sql.Literal(PARTICIPANT_ID_POOL_VERSION),
        sequence_name=sql.Literal(sequence_name),
        sequence_regclass=sql.Literal(f"public.{sequence_name}"),
        values=values,
        schema_revision=sql.Literal(SCHEMA_REVISION),
    )


def build_pool_preparation_sql(event_id: UUID) -> str:
    """人手用psql出力を維持し、共通SQLを独立transactionで包む。"""

    return (
        "-- 人が対象DBと旧APIのjoin停止を確認し、psqlで一度だけ実行する。\n"
        f"-- 先にAlembic revision {SCHEMA_REVISION}を人手で適用する。DB接続情報は含めない。\n"
        "-- 適用後、application roleへpool/slotのSELECTとsequenceのUSAGEを付与する。\n"
        "\\set ON_ERROR_STOP on\nBEGIN;\n"
        + build_pool_preparation_statement(event_id).as_string()
        + "COMMIT;\n"
    )


def write_pool_preparation_sql(request: PoolSqlRequest) -> None:
    """既存artifactを上書きせず、所有者だけが読めるSQLファイルを保存する。"""

    statement = build_pool_preparation_sql(request.event_id)
    descriptor = os.open(request.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(statement)


def main() -> None:
    """SQL生成のみを行い、DB接続・DDL適用・既存参加者更新を実行しない。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    logfire.configure(send_to_logfire=False)
    try:
        request = PoolSqlRequest.model_validate(vars(arguments))
        logfire.info("participant ID pool SQL generation started", event_id=request.event_id)
        write_pool_preparation_sql(request)
    except (OSError, ValidationError):
        logfire.exception("participant ID pool SQL generation failed")
        raise
    logfire.info("participant ID pool SQL generation completed", event_id=request.event_id)


if __name__ == "__main__":
    main()
