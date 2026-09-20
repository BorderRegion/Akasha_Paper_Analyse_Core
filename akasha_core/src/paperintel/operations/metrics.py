"""Durable, bounded-label Prometheus projections (doc 06 §8).

Inventory families are gauges: status transitions and retention can decrease
them. Transport counters come from durable attempt events. Duration summaries
describe retained completed records, not an unbounded historical accumulator.
"""

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, SummaryMetricFamily
from sqlalchemy import func, select

from paperintel.database.models import (
    ClaimRow,
    EvidenceRow,
    JobRow,
    ModelCallRow,
    TaskRow,
    VerificationRow,
)


def export_metrics(session, status, *, settings=None):
    registry = CollectorRegistry()
    families = []

    def gauge(name, value, labels=(), samples=()):
        family = GaugeMetricFamily(name, name.replace("_", " "), labels=labels)
        if labels:
            for keys, count in samples:
                family.add_metric([str(k) for k in keys], count)
        else:
            family.add_metric([], value)
        families.append(family)

    def counts(name, model, columns=(), where=None):
        family = GaugeMetricFamily(
            name, name.replace("_", " "), labels=[label for label, _ in columns]
        )
        attrs = [attr for _, attr in columns]
        query = select(*attrs, func.count()).select_from(model)
        if where is not None:
            query = query.where(where)
        if attrs:
            query = query.group_by(*attrs)
        for row in session.execute(query):
            family.add_metric([str(k) for k in row[:-1]], row[-1])
        families.append(family)

    counts("jobs_total", JobRow)
    gauge(
        "jobs_active",
        session.scalar(
            select(func.count())
            .select_from(JobRow)
            .where(
                JobRow.state.in_(["PENDING", "QUEUED", "RUNNING", "WAITING", "RETRYING", "BLOCKED"])
            )
        )
        or 0,
    )
    counts("jobs_failed_total", JobRow, where=JobRow.state == "FAILED")
    counts("tasks_total", TaskRow, (("type", TaskRow.task_type), ("state", TaskRow.state)))
    counts(
        "provider_requests_total",
        ModelCallRow,
        (
            ("provider", ModelCallRow.provider_id),
            ("model", ModelCallRow.model_id),
            ("status", ModelCallRow.transport_status),
        ),
    )
    counts(
        "provider_rate_limit_total",
        ModelCallRow,
        where=ModelCallRow.transport_status == "RATE_LIMITED",
    )
    counts(
        "provider_schema_fail_total",
        ModelCallRow,
        where=ModelCallRow.schema_status.in_(
            ["JSON_INVALID", "SCHEMA_INVALID", "SEMANTIC_INVALID", "EVIDENCE_INVALID"]
        ),
    )
    counts("evidence_created_total", EvidenceRow, (("type", EvidenceRow.evidence_type),))
    counts("claims_created_total", ClaimRow, (("type", ClaimRow.claim_type),))
    counts("claims_status_total", ClaimRow, (("support_state", ClaimRow.support_state),))
    counts(
        "verification_total",
        VerificationRow,
        (("verdict", VerificationRow.verdict), ("type", VerificationRow.verifier_type)),
    )
    for name, predicate in (
        ("ocr_pages_total", EvidenceRow.source_method.in_(["OCR", "MIXED_NATIVE_OCR"])),
        ("ocr_low_confidence_total", EvidenceRow.ocr_confidence < 0.8),
    ):
        pages = (
            select(EvidenceRow.paper_version_id, EvidenceRow.page_start)
            .where(predicate)
            .distinct()
            .subquery()
        )
        family = CounterMetricFamily(name, "Distinct retained OCR pages")
        family.add_metric([], session.scalar(select(func.count()).select_from(pages)) or 0)
        families.append(family)
    for name, model, attrs, labels, duration in (
        (
            "task_duration_seconds",
            TaskRow,
            [TaskRow.task_type],
            ["type"],
            func.extract("epoch", TaskRow.finished_at - TaskRow.started_at),
        ),
        (
            "provider_latency_seconds",
            ModelCallRow,
            [ModelCallRow.provider_id, ModelCallRow.model_id],
            ["provider", "model"],
            ModelCallRow.latency_ms / 1000.0,
        ),
    ):
        family = SummaryMetricFamily(
            name, "Duration of retained completed observations", labels=labels
        )
        for row in session.execute(
            select(*attrs, func.count(duration), func.sum(duration))
            .select_from(model)
            .where(duration >= 0)
            .group_by(*attrs)
        ):
            family.add_metric([str(k) for k in row[:-2]], row[-2], float(row[-1]))
        families.append(family)
    gauge(
        "disk_bytes", None, ["category"], [([k], v) for k, v in status["disk"]["breakdown"].items()]
    )
    gauge("queue_depth", status["queue"]["pending"])
    from paperintel.workflow.celery_app import get_celery_app, tasks_eager

    if tasks_eager():
        active = 1
    else:
        try:
            active = len(get_celery_app().control.inspect(timeout=1.0).ping() or {})
        except Exception:
            # Unknown is NaN, not a fabricated zero/healthy worker count.
            active = float("nan")
    gauge("worker_active", active)

    from paperintel.config.settings import get_settings
    from paperintel.operations.provider_observations import metric_families

    observed = list(metric_families((settings or get_settings()).core.data_dir))
    replacements = {family.name for family in observed}
    families = [f for f in families if f.name not in replacements] + observed

    class Collector:
        def collect(self):
            return iter(families)

    registry.register(Collector())
    return generate_latest(registry).decode("utf-8")
