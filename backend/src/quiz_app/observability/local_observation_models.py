"""ローカル観測の保存契約。欠測と測定済みゼロを区別する。"""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from quiz_app.observability.database_snapshot import DatabaseWaitSnapshot

ApiInstance = Literal["api", "api-secondary"]


class ObservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ObservationManifest(ObservationModel):
    format: Literal["local-server-observation-v1"] = "local-server-observation-v1"
    started_at: AwareDatetime
    duration_seconds: int = Field(ge=1, le=7200)
    instances: list[ApiInstance] = Field(min_length=1, max_length=2)
    scrape_interval_seconds: float = Field(default=1.0, gt=0)
    auxiliary_interval_seconds: float = Field(default=15.0, gt=0)
    scope: str = "API process metrics。結果子workerは含まない。DB/containerは別の疎な観測。"

    @model_validator(mode="after")
    def require_unique_instances(self) -> "ObservationManifest":
        if self.instances not in (["api"], ["api", "api-secondary"]):
            raise ValueError("invalid_observation_instances")
        return self


class TimedObservation(ObservationModel):
    started_at: AwareDatetime
    monotonic_seconds: float = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


class MetricsObservation(TimedObservation):
    kind: Literal["metrics"] = "metrics"
    instance: ApiInstance
    status: Literal["ok", "unavailable"]
    exposition: str | None = None
    failure: Literal["timeout", "transport", "http_status"] | None = None

    @model_validator(mode="after")
    def require_status_payload_agreement(self) -> "MetricsObservation":
        if self.status == "ok" and (self.exposition is None or self.failure is not None):
            raise ValueError("metrics_success_requires_exposition")
        if self.status == "unavailable" and (self.exposition is not None or self.failure is None):
            raise ValueError("metrics_unavailable_requires_failure")
        return self


class DatabaseObservation(TimedObservation):
    kind: Literal["database"] = "database"
    status: Literal["ok", "unavailable"]
    snapshot: DatabaseWaitSnapshot | None = None
    failure: Literal["timeout", "process_exit", "invalid_output", "spawn"] | None = None


class CollectorFinished(ObservationModel):
    kind: Literal["collector_finished"] = "collector_finished"
    instance: ApiInstance
    finished_at: AwareDatetime
    reason: Literal["duration", "parent_closed", "signal"]


ObservationRecord = Annotated[
    MetricsObservation | DatabaseObservation | CollectorFinished, Field(discriminator="kind")
]
observation_adapter = TypeAdapter(ObservationRecord)


class ContainerSample(BaseModel):
    # Dockerの別名入力と、保存済みJSONのfield名を同じモデルで再読込する。
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    name: str = Field(alias="Name")
    cpu_percent: str = Field(alias="CPUPerc")
    memory_usage: str = Field(alias="MemUsage")
    memory_percent: str = Field(alias="MemPerc")
    pids: str = Field(alias="PIDs")


class ContainersObservation(TimedObservation):
    kind: Literal["containers"] = "containers"
    status: Literal["ok", "unavailable"]
    samples: list[ContainerSample] = Field(default_factory=list)
    failure: Literal["timeout", "process_exit", "invalid_output", "spawn"] | None = None


class ProcessCompletion(ObservationModel):
    instance: ApiInstance
    exit_code: int | None


class ObservationCompletion(ObservationModel):
    finished_at: AwareDatetime
    interrupted: bool
    processes: list[ProcessCompletion]

    @model_validator(mode="after")
    def require_unique_processes(self) -> "ObservationCompletion":
        if len({value.instance for value in self.processes}) != len(self.processes):
            raise ValueError("duplicate_process_completion")
        return self


class MeasurementWindow(ObservationModel):
    """測定区間をUTCへ正規化し、観測のライフサイクルと分ける。"""

    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def require_ordered_utc_window(self) -> "MeasurementWindow":
        self.start_at = self.start_at.astimezone(UTC)
        self.end_at = self.end_at.astimezone(UTC)
        if self.end_at <= self.start_at:
            raise ValueError("measurement_window_end_must_follow_start")
        return self


class MetricTotal(ObservationModel):
    name: str
    labels: dict[str, str]
    delta: float
    intervals: int


class HistogramTotal(ObservationModel):
    name: str
    labels: dict[str, str]
    count: float
    sum: float
    buckets: dict[str, float]
    intervals: int
    p95: float | None
    p95_lower_bound: float | None
    p95_upper_bound: float | None


class GaugeRange(ObservationModel):
    name: str
    labels: dict[str, str]
    minimum: float
    maximum: float
    last: float
    samples: int


class ProcessAnalysis(ObservationModel):
    instance: ApiInstance
    samples: int = 0
    excluded_before_window: int = 0
    excluded_after_window: int = 0
    excluded_boundary_overlaps: int = 0
    preceding_excluded_sample_at: datetime | None = None
    preceding_excluded_sample_status: Literal["ok", "unavailable"] | None = None
    counter_baseline_at: datetime | None = None
    counter_baseline_completed_at: datetime | None = None
    last_counter_sample_at: datetime | None = None
    last_counter_sample_completed_at: datetime | None = None
    valid_counter_samples: int = 0
    leading_unobserved_seconds_upper: float | None = None
    trailing_unobserved_seconds_upper: float | None = None
    window_coverage_sufficient: bool = False
    series_discontinuities: int = 0
    created_counter_series: int = 0
    created_histogram_series: int = 0
    created_metadata_changes: int = 0
    unavailable: int = 0
    invalid_records: int = 0
    finished: bool = False
    first_sample_at: datetime | None = None
    last_sample_at: datetime | None = None
    collector_max_seconds: float | None = None
    largest_sample_gap_seconds: float | None = None
    restarts: list[datetime] = Field(default_factory=list)
    counter_resets: int = 0
    histogram_resets: int = 0
    identity_missing_samples: int = 0
    unbased_series: int = 0
    counters: list[MetricTotal] = Field(default_factory=list)
    histograms: list[HistogramTotal] = Field(default_factory=list)
    gauges: list[GaugeRange] = Field(default_factory=list)
    database_samples: int = 0
    database_unavailable: int = 0


class ServerAnalysis(ObservationModel):
    format: Literal["local-server-analysis-v3"] = "local-server-analysis-v3"
    collector_lifecycle_complete: bool
    measurement_window: MeasurementWindow | None
    measurement_usable: bool
    measurement_quality_failures: list[str]
    coverage_max_gap_seconds: float
    processes: list[ProcessAnalysis]
    counters: list[MetricTotal]
    histograms: list[HistogramTotal]
    incompatible_histograms: list[str]
    scope: str = (
        "窓指定時はscrape開始〜完了が全て窓内の標本だけを選択し、"
        "各API内の同一processの観測済み区間を差分化し合算。"
        "最初の窓内標本はbaselineとして差分から除外。"
        "外側baselineとの差分、時刻比例按分、両端の不足補完は行わない。"
        "measurement_usableは明示coverage上限内の部分区間としての利用可否であり、"
        "要求窓の正確な全件数やアプリ合格ではない。"
        "窓未指定はcollector全時間の診断だけ。再起動、"
        "reset、消失/再出現、生成根拠なしの初出系列は補完しない。"
        "同一processの連続有効標本で未見系列の_createdが前scrape開始〜現完了かつ窓内にあり、"
        "生成metadataが観測中に変化しない場合のみ生成時0から初値を計上する。"
        "欠測/不正標本で差分の連続性を切断し、"
        "観測されなかった再起動を除外できるとはしない。p95は合算bucketの補間値。"
        "gaugeはprocess別、結果子workerはAPI metrics外。DBは件数/欠測だけ、"
        "containerはcontainers.jsonlの生標本を別途確認。host/browser時計と合算しない。"
        "指定UTC窓とcollector時計の対応は入力側の前提であり、この集計は時計差を校正しない。"
    )
