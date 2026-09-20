"""Shared API projections from durable provider events, never fresh-client stats."""

import time
from collections import Counter, defaultdict

from paperintel.providers.runtime import read_events


def snapshots(data_dir, *, now=None):
    now = now if now is not None else time.time()
    grouped = defaultdict(list)
    for event in read_events(data_dir):
        grouped[event["provider"]].append(event)
    result = {}
    for provider, events in grouped.items():
        attempts = [e for e in events if e["kind"] == "attempt"]
        schema = [e for e in events if e["kind"] == "schema"]
        citations = [e for e in events if e["kind"] == "citation"]
        latencies = sorted(e["value"] for e in events if e["kind"] == "latency")
        latest = {e["kind"]: e for e in events}
        canary = latest.get("canary", {})
        canary_state = canary.get("canary_state", "UNKNOWN")
        if canary and now - canary["occurred"] > 86400:
            canary_state = "STALE"
        breaker = latest.get("breaker", {}).get("circuit_breaker_state", "UNKNOWN")
        # Stale breaker observations cannot report a current CLOSED/healthy state.
        if now - latest.get("breaker", {}).get("occurred", 0) > 300:
            breaker = "UNKNOWN"
        errors = sum(e.get("status") != "OK" for e in attempts)
        availability = "UNKNOWN"
        if breaker == "OPEN":
            availability = "UNAVAILABLE"
        elif canary_state in {"FAILED", "STALE"} or breaker == "HALF_OPEN":
            availability = "DEGRADED"
        elif attempts:
            availability = "DEGRADED" if errors / len(attempts) > 0.2 else "HEALTHY"
            if now - attempts[-1]["occurred"] > 300:
                availability = "UNKNOWN"
        result[provider] = {
            "availability_state": availability,
            "canary_state": canary_state,
            "circuit_breaker_state": breaker,
            "total_calls": len(attempts),
            "transport_error_rate": errors / len(attempts) if attempts else None,
            "schema_pass_rate": sum(e["success"] for e in schema) / len(schema) if schema else None,
            "citation_pass_rate": sum(e["success"] for e in citations) / len(citations)
            if citations
            else None,
            "unsupported_claim_rate": min(
                1, sum(e["kind"] == "unsupported" for e in events) / len(schema)
            )
            if schema
            else None,
            "latency_p50": latencies[(len(latencies) - 1) // 2] if latencies else None,
            "latency_p90": latencies[min(len(latencies) - 1, int(len(latencies) * 0.9))]
            if latencies
            else None,
            "observation_source": "durable_provider_events",
        }
    return result


def metric_families(data_dir):
    from prometheus_client.core import CounterMetricFamily, SummaryMetricFamily

    events = read_events(data_dir)
    requests = Counter(
        (e["provider"], str(e.get("model", "unknown")), e["status"])
        for e in events
        if e["kind"] == "attempt"
    )
    requests_family = CounterMetricFamily(
        "provider_requests_total",
        "Transport attempts including retries",
        labels=["provider", "model", "status"],
    )
    for labels, count in requests.items():
        requests_family.add_metric(list(labels), count)
    yield requests_family
    for name, predicate in (
        (
            "provider_rate_limit_total",
            lambda e: e["kind"] == "attempt" and e.get("status") in {"LLM_002", "PROVIDER_002"},
        ),
        ("provider_schema_fail_total", lambda e: e["kind"] == "schema" and not e.get("success")),
        ("ocr_pages_total", lambda e: e["kind"] == "ocr"),
        (
            "ocr_low_confidence_total",
            lambda e: e["kind"] == "ocr" and (e.get("value") is None or e["value"] < 0.8),
        ),
    ):
        family = CounterMetricFamily(name, "Durable provider observations")
        family.add_metric([], sum(predicate(e) for e in events))
        yield family
    latencies = defaultdict(list)
    for event in events:
        if event["kind"] == "latency":
            latencies[(event["provider"], str(event.get("model", "unknown")))].append(
                event["value"] / 1000
            )
    family = SummaryMetricFamily(
        "provider_latency_seconds",
        "Transport attempt latency including failures",
        labels=["provider", "model"],
    )
    for labels, values in latencies.items():
        family.add_metric(list(labels), len(values), sum(values))
    yield family
