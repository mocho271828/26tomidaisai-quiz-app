from quiz_app.observability.metrics import QuizMetrics


def test_realtime_metrics_separate_delivery_build_and_degraded_state() -> None:
    metrics = QuizMetrics()
    labels = {"audience": "participant", "kind": "personal_result", "outcome": "sent"}
    metrics.record_realtime_delivery(**labels, duration_seconds=0.25)
    metrics.record_realtime_delivery(
        audience="participant", kind="personal_result", outcome="discarded"
    )
    metrics.record_realtime_build(
        audience="participant", kind="personal_result", outcome="success", duration_seconds=0.5
    )
    metrics.record_realtime_build(
        audience="participant", kind="personal_result", outcome="deferred"
    )
    metrics.set_realtime_redis_degraded(audience="participant", is_degraded=True)

    assert metrics.registry.get_sample_value("quiz_realtime_delivery_total", labels) == 1
    assert (
        metrics.registry.get_sample_value("quiz_realtime_delivery_duration_seconds_sum", labels)
        == 0.25
    )
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_delivery_duration_seconds_count", {**labels, "outcome": "discarded"}
        )
        is None
    )
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_build_duration_seconds_sum", {**labels, "outcome": "success"}
        )
        == 0.5
    )
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_build_total", {**labels, "outcome": "deferred"}
        )
        == 1
    )
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_build_duration_seconds_count", {**labels, "outcome": "deferred"}
        )
        is None
    )
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_redis_degraded", {"audience": "participant"}
        )
        == 1
    )
    metrics.set_realtime_redis_degraded(audience="participant", is_degraded=False)
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_redis_degraded", {"audience": "participant"}
        )
        == 0
    )
    assert QuizMetrics().registry.get_sample_value("quiz_realtime_delivery_total", labels) is None


def test_realtime_metrics_preserve_timeout_duration_and_bounded_labels() -> None:
    metrics = QuizMetrics()
    metrics.record_realtime_delivery(
        audience="operator", kind="heartbeat", outcome="timeout", duration_seconds=0.8
    )
    metrics.record_realtime_build(
        audience="operator", kind="common", outcome="timeout", duration_seconds=1.2
    )
    for family in metrics.registry.collect():
        if not family.name.startswith("quiz_realtime_"):
            continue
        for sample in family.samples:
            assert set(sample.labels) <= {"audience", "kind", "outcome", "le"}
    assert (
        metrics.registry.get_sample_value(
            "quiz_realtime_delivery_duration_seconds_sum",
            {"audience": "operator", "kind": "heartbeat", "outcome": "timeout"},
        )
        == 0.8
    )
