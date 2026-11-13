"""採番を進めず、配置対象開催の固定IDリストとapplication権限を検査する。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from quiz_app.models.participant_ids import PARTICIPANT_ID_CAPACITY, PARTICIPANT_ID_POOL_VERSION

PARTICIPANT_ID_READINESS = text("""
    SELECT pool.capacity, pool.version,
           slots.slot_count, slots.label_count, slots.first_ordinal, slots.last_ordinal,
           slots.invalid_label_count,
           sequence.min_value, sequence.max_value, sequence.start_value,
           sequence.increment_by, sequence.cache_size, sequence.cycle,
           sequence.last_value AS sequence_last_value,
           sequence.data_type = 'integer'::regtype AS integer_sequence,
           sequence_class.relpersistence = 'p' AS persistent_sequence,
           has_table_privilege(current_user, 'public.participant_id_pools', 'SELECT')
             AND has_table_privilege(current_user, 'public.participant_id_slots', 'SELECT')
             AND NOT has_table_privilege(current_user, 'public.participant_id_pools',
                                         'INSERT,UPDATE,DELETE,TRUNCATE')
             AND NOT has_table_privilege(current_user, 'public.participant_id_slots',
                                         'INSERT,UPDATE,DELETE,TRUNCATE')
             AS immutable_pool_access,
           has_sequence_privilege(current_user, sequence_class.oid, 'USAGE')
             AND NOT has_sequence_privilege(current_user, sequence_class.oid, 'UPDATE')
             AS sequence_access
    FROM public.participant_id_pools AS pool
    CROSS JOIN LATERAL (
        SELECT count(*) AS slot_count, count(DISTINCT public_label) AS label_count,
               min(ordinal) AS first_ordinal, max(ordinal) AS last_ordinal,
               count(*) FILTER (WHERE public_label !~ '^[A-Z]{3}$') AS invalid_label_count
        FROM public.participant_id_slots WHERE event_id = pool.event_id
    ) AS slots
    LEFT JOIN pg_catalog.pg_sequences AS sequence
      ON sequence.schemaname = 'public' AND sequence.sequencename = :sequence_name
    LEFT JOIN pg_catalog.pg_class AS sequence_class
      ON sequence_class.oid = to_regclass(:sequence_regclass)
    WHERE pool.event_id = :event_id
""")


class ParticipantIdReadinessRow(BaseModel):
    """DB由来の値を厳密に検証し、欠落したsequenceも未準備として扱う。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    capacity: int = Field(ge=0, le=PARTICIPANT_ID_CAPACITY)
    version: str
    slot_count: int
    label_count: int
    first_ordinal: int | None
    last_ordinal: int | None
    invalid_label_count: int
    min_value: int | None
    max_value: int | None
    start_value: int | None
    increment_by: int | None
    cache_size: int | None
    cycle: bool | None
    sequence_last_value: int | None
    integer_sequence: bool | None
    persistent_sequence: bool | None
    immutable_pool_access: bool
    sequence_access: bool | None

    def is_ready(self) -> bool:
        """固定slotの完全性、非循環sequence、次回採番余地を確認する。"""

        return (
            self.version == PARTICIPANT_ID_POOL_VERSION
            and self.capacity > 0
            and self.slot_count == self.label_count == self.last_ordinal == self.capacity
            and self.first_ordinal == 1
            and self.invalid_label_count == 0
            and self.min_value == self.start_value == self.increment_by == self.cache_size == 1
            and self.max_value == self.capacity
            and self.cycle is False
            and self.integer_sequence is True
            and self.persistent_sequence is True
            and self.immutable_pool_access
            and self.sequence_access is True
            and (self.sequence_last_value is None or 1 <= self.sequence_last_value < self.capacity)
        )


def inspect_participant_id_readiness(connection: Connection, event_id: UUID) -> bool:
    """SELECTだけで開催と固定名を照合し、nextval/setvalや修復を実行しない。"""

    sequence_name = f"participant_id_{event_id.hex}"
    row = (
        connection.execute(
            PARTICIPANT_ID_READINESS,
            {
                "event_id": event_id,
                "sequence_name": sequence_name,
                "sequence_regclass": f"public.{sequence_name}",
            },
        )
        .mappings()
        .one_or_none()
    )
    return row is not None and ParticipantIdReadinessRow.model_validate(dict(row)).is_ready()
