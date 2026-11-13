from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

OPTION_LABELS = ("A", "B", "C", "D")
EXPECTED_RAMP_TARGETS = (100, 300, 500, 600)
EXPECTED_RAMP_TARGET_OFFSETS_SECONDS = (150, 300, 450, 600)
EXPECTED_OPTION_COUNTS = (126, 125, 125, 124)
EXPECTED_ROUND_CONTRACT = (
    (1, 900, "mixed", 500, EXPECTED_OPTION_COUNTS, "none", "none", None, None, None, None, None),
    (2, 1260, "mixed", 500, EXPECTED_OPTION_COUNTS, "none", "none", None, None, None, None, None),
    (
        3,
        1620,
        "concentrated",
        500,
        EXPECTED_OPTION_COUNTS,
        "all",
        "none",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        4,
        1980,
        "concentrated",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "receipt-discard",
        1,
        50,
        "Stored",
        2500,
        "NotStored",
    ),
    (
        5,
        2340,
        "mixed",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "redis-unavailable",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        6,
        2700,
        "mixed",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "redis-recovered",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        7,
        3060,
        "mixed",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "api-secondary-unavailable",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        8,
        3420,
        "concentrated",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "api-secondary-recovered",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        9,
        3780,
        "mixed",
        500,
        EXPECTED_OPTION_COUNTS,
        "none",
        "accumulated",
        None,
        None,
        None,
        None,
        None,
    ),
    (
        10,
        4140,
        "concentrated",
        500,
        EXPECTED_OPTION_COUNTS,
        "all",
        "none",
        None,
        None,
        None,
        None,
        None,
    ),
)
EXPECTED_FAULT_SCHEDULE = (
    ("redis", "disable", 2280),
    ("redis", "enable", 2580),
    ("api-secondary", "disable", 3000),
    ("api-secondary", "enable", 3300),
)


class StrictScenarioModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LoadTestOptionCountsManifest(StrictScenarioModel):
    a: StrictInt = Field(ge=0)
    b: StrictInt = Field(ge=0)
    c: StrictInt = Field(ge=0)
    d: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_expected_counts(self) -> Self:
        if (self.a, self.b, self.c, self.d) != EXPECTED_OPTION_COUNTS:
            raise ValueError("回答oracleの期待件数は[126,125,125,124]に固定です")
        return self


class LoadTestVirtualAnswerAssignmentManifest(StrictScenarioModel):
    vu_id: StrictInt = Field(ge=1, le=600)
    option: Literal["A", "B", "C", "D"]


class LoadTestAnswerOracleManifest(StrictScenarioModel):
    modulo_vu_start: StrictInt
    modulo_vu_end: StrictInt
    modulo_option_cycle: tuple[
        Literal["A"],
        Literal["B"],
        Literal["C"],
        Literal["D"],
    ]
    fixed_virtual_answers: tuple[LoadTestVirtualAnswerAssignmentManifest, ...] = Field(
        min_length=6, max_length=6
    )
    mixed_virtual_vu_end: StrictInt
    mixed_additional_vu_ids: tuple[Literal[599], Literal[600]]
    concentrated_virtual_vu_end: StrictInt
    non_answering_vu_start: StrictInt
    non_answering_vu_end: StrictInt
    expected_option_counts: LoadTestOptionCountsManifest

    @model_validator(mode="after")
    def validate_fixed_oracle(self) -> Self:
        fixed_answers = tuple(
            (assignment.vu_id, assignment.option) for assignment in self.fixed_virtual_answers
        )
        if (
            self.modulo_vu_start != 1
            or self.modulo_vu_end != 496
            or self.modulo_option_cycle != OPTION_LABELS
            or fixed_answers
            != ((497, "A"), (498, "A"), (499, "B"), (500, "C"), (599, "B"), (600, "C"))
            or self.mixed_virtual_vu_end != 498
            or self.mixed_additional_vu_ids != (599, 600)
            or self.concentrated_virtual_vu_end != 500
            or self.non_answering_vu_start != 501
            or self.non_answering_vu_end != 598
        ):
            raise ValueError("回答oracleは承認済み600 VU分布に固定です")
        return self


class LoadTestLifecycleManifest(StrictScenarioModel):
    virtual_sessions: StrictInt
    target_total_sessions: StrictInt
    ramp_start_seconds: StrictInt
    ramp_end_seconds: StrictInt
    ramp_targets: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    ramp_target_offsets_seconds: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    measurement_start_seconds: StrictInt
    measurement_end_seconds: StrictInt
    ramp_down_start_seconds: StrictInt
    ramp_down_end_seconds: StrictInt
    exclusive_window_confirmation_required: bool

    @model_validator(mode="after")
    def validate_fixed_lifecycle(self) -> Self:
        actual = (
            self.virtual_sessions,
            self.target_total_sessions,
            self.ramp_start_seconds,
            self.ramp_end_seconds,
            self.ramp_targets,
            self.ramp_target_offsets_seconds,
            self.measurement_start_seconds,
            self.measurement_end_seconds,
            self.ramp_down_start_seconds,
            self.ramp_down_end_seconds,
            self.exclusive_window_confirmation_required,
        )
        expected = (
            600,
            600,
            0,
            600,
            EXPECTED_RAMP_TARGETS,
            EXPECTED_RAMP_TARGET_OFFSETS_SECONDS,
            900,
            4500,
            4500,
            4800,
            True,
        )
        if actual != expected:
            raise ValueError("load lifecycleは承認済み80分scheduleに固定です")
        return self


class LoadTestProtocolManifest(StrictScenarioModel):
    join_token_characters: StrictInt
    independent_cookie_jar_per_session: bool
    independent_websocket_per_session: bool
    heartbeat_interval_seconds: StrictInt
    clock_samples_per_sync: StrictInt
    clock_sync_interval_seconds: StrictInt
    clock_sample_timeout_ms: StrictInt
    clock_sample_selection: Literal["shortest-rtt"]
    clock_sample_max_rtt_ms: StrictInt
    polling_interval_seconds: StrictInt
    websocket_reconnect_interval_seconds: StrictInt
    static_asset_fetches_per_session: StrictInt
    refetch_projection_after_invalidation: bool
    send_readiness: bool
    query_status_after_unknown_response: bool
    client_answer_id_scheme: Literal["uuid5(run_id,question_id:vu_id)"]

    @model_validator(mode="after")
    def validate_fixed_protocol(self) -> Self:
        actual = (
            self.join_token_characters,
            self.independent_cookie_jar_per_session,
            self.independent_websocket_per_session,
            self.heartbeat_interval_seconds,
            self.clock_samples_per_sync,
            self.clock_sync_interval_seconds,
            self.clock_sample_timeout_ms,
            self.clock_sample_selection,
            self.clock_sample_max_rtt_ms,
            self.polling_interval_seconds,
            self.websocket_reconnect_interval_seconds,
            self.static_asset_fetches_per_session,
            self.refetch_projection_after_invalidation,
            self.send_readiness,
            self.query_status_after_unknown_response,
            self.client_answer_id_scheme,
        )
        expected = (
            43,
            True,
            True,
            3,
            3,
            30,
            1000,
            "shortest-rtt",
            500,
            1,
            5,
            1,
            True,
            True,
            True,
            "uuid5(run_id,question_id:vu_id)",
        )
        if actual != expected:
            raise ValueError("session protocolは承認済み固定値と一致しません")
        return self


class LoadTestAcceptanceThresholdsManifest(StrictScenarioModel):
    sustained_total_sessions: StrictInt
    sustained_duration_seconds: StrictInt
    normal_participant_websockets: StrictInt
    target_count: StrictInt
    normal_expected_ready_count: StrictInt
    ready_count_minimum: StrictInt
    asset_failure_maximum: StrictInt
    expected_answer_count: StrictInt
    round_start_width_max_ms: StrictInt
    notification_refresh_over_one_second_maximum: StrictInt
    http_5xx_maximum: StrictInt
    recovery_seconds_maximum: StrictInt
    missed_rounds_maximum: StrictInt
    duplicate_answers_maximum: StrictInt
    misleading_successes_maximum: StrictInt
    unresolved_answers_maximum: StrictInt
    replay_same_answer_id_required: bool
    replay_row_change_maximum: StrictInt
    latency_mode: Literal["first-run-calibration-only"]
    latency_report_statistics: tuple[
        Literal["p50"],
        Literal["p95"],
        Literal["p99"],
        Literal["max"],
    ]
    hard_latency_threshold_ms: None

    @model_validator(mode="after")
    def validate_fixed_thresholds(self) -> Self:
        actual = (
            self.sustained_total_sessions,
            self.sustained_duration_seconds,
            self.normal_participant_websockets,
            self.target_count,
            self.normal_expected_ready_count,
            self.ready_count_minimum,
            self.asset_failure_maximum,
            self.expected_answer_count,
            self.round_start_width_max_ms,
            self.notification_refresh_over_one_second_maximum,
            self.http_5xx_maximum,
            self.recovery_seconds_maximum,
            self.missed_rounds_maximum,
            self.duplicate_answers_maximum,
            self.misleading_successes_maximum,
            self.unresolved_answers_maximum,
            self.replay_same_answer_id_required,
            self.replay_row_change_maximum,
            self.latency_mode,
            self.latency_report_statistics,
            self.hard_latency_threshold_ms,
        )
        expected = (
            600,
            3600,
            600,
            600,
            600,
            588,
            11,
            500,
            1000,
            5,
            0,
            120,
            0,
            0,
            0,
            0,
            True,
            0,
            "first-run-calibration-only",
            ("p50", "p95", "p99", "max"),
            None,
        )
        if actual != expected:
            raise ValueError("acceptance thresholdは承認済み固定値と一致しません")
        return self


class LoadTestResourceAbortManifest(StrictScenarioModel):
    generator_cpu_percent_over: StrictInt
    generator_memory_percent_over: StrictInt
    scheduler_lag_ms_over: StrictInt
    consecutive_seconds: StrictInt

    @model_validator(mode="after")
    def validate_fixed_abort_thresholds(self) -> Self:
        actual = (
            self.generator_cpu_percent_over,
            self.generator_memory_percent_over,
            self.scheduler_lag_ms_over,
            self.consecutive_seconds,
        )
        if actual != (80, 80, 50, 30):
            raise ValueError("generator abort thresholdは承認済み固定値と一致しません")
        return self


class LoadTestRoundScenarioManifest(StrictScenarioModel):
    number: StrictInt = Field(ge=1, le=10)
    start_offset_seconds: StrictInt
    answer_population: Literal["mixed", "concentrated"]
    virtual_answer_count: StrictInt
    expected_option_counts: LoadTestOptionCountsManifest
    replay: Literal["none", "all"]
    condition: Literal[
        "none",
        "receipt-discard",
        "redis-unavailable",
        "redis-recovered",
        "api-secondary-unavailable",
        "api-secondary-recovered",
        "accumulated",
    ]
    receipt_discard_vu_start: StrictInt | None
    receipt_discard_vu_end: StrictInt | None
    receipt_discard_only_when_status: Literal["Stored"] | None
    answer_status_delay_ms: StrictInt | None
    retry_only_when_status: Literal["NotStored"] | None


class LoadTestFaultActionManifest(StrictScenarioModel):
    target: Literal["redis", "api-secondary"]
    action: Literal["disable", "enable"]
    offset_seconds: StrictInt


class LoadTestScenarioManifest(StrictScenarioModel):
    lifecycle: LoadTestLifecycleManifest
    answer_oracle: LoadTestAnswerOracleManifest
    protocol: LoadTestProtocolManifest
    thresholds: LoadTestAcceptanceThresholdsManifest
    resource_abort: LoadTestResourceAbortManifest
    rounds: tuple[
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
        LoadTestRoundScenarioManifest,
    ]
    fault_schedule: tuple[
        LoadTestFaultActionManifest,
        LoadTestFaultActionManifest,
        LoadTestFaultActionManifest,
        LoadTestFaultActionManifest,
    ]

    @model_validator(mode="after")
    def validate_rounds_and_fault_schedule(self) -> Self:
        actual_rounds = tuple(
            (
                round_.number,
                round_.start_offset_seconds,
                round_.answer_population,
                round_.virtual_answer_count,
                (
                    round_.expected_option_counts.a,
                    round_.expected_option_counts.b,
                    round_.expected_option_counts.c,
                    round_.expected_option_counts.d,
                ),
                round_.replay,
                round_.condition,
                round_.receipt_discard_vu_start,
                round_.receipt_discard_vu_end,
                round_.receipt_discard_only_when_status,
                round_.answer_status_delay_ms,
                round_.retry_only_when_status,
            )
            for round_ in self.rounds
        )
        actual_faults = tuple(
            (fault.target, fault.action, fault.offset_seconds) for fault in self.fault_schedule
        )
        if actual_rounds != EXPECTED_ROUND_CONTRACT:
            raise ValueError("10 Round scenarioが承認済みcontractと一致しません")
        if actual_faults != EXPECTED_FAULT_SCHEDULE:
            raise ValueError("障害注入scheduleが承認済みcontractと一致しません")
        return self
