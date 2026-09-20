"""web.ui — the operations UI (P12, doc 04 §15).

Server-rendered HTML (no build step, no client framework) so the
operations view works in a local deployment with zero tooling. It shows
exactly what doc 04 §15 requires — system health, queue, workers,
providers, disk, DB, Redis, module health, recent failures — plus a paper
page with analysis health, pipeline stages, evidence quality, claim
states, high-risk audit items and model/pipeline versions.

All values come from paperintel.services.read_models: the UI is another
consumer of the same layer, never a separate implementation.
"""

from __future__ import annotations

import html
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paperintel.config.settings import AppConfig
from paperintel.database.models import EvidenceRow, JobRow, TaskRow
from paperintel.schemas.enums import DataQualityState, SupportState, TaskState


def _esc(value: Any) -> str:
    return html.escape(str(value))


def _row(label: str, value: Any) -> str:
    return f"<tr><th>{_esc(label)}</th><td>{_esc(value)}</td></tr>"


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{_esc(title)}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1b1f23; }}
 h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.1rem; margin-top: 1.6rem; }}
 table {{ border-collapse: collapse; margin: .4rem 0 1rem; }}
 th, td {{ border: 1px solid #d5d9dd; padding: .3rem .6rem; text-align: left; font-size: .9rem; }}
 th {{ background: #f4f6f8; }}
 .ok {{ color: #146c2e; font-weight: 600; }}
 .warn {{ color: #8a6100; font-weight: 600; }}
 .bad {{ color: #a4161a; font-weight: 600; }}
 code {{ background: #f4f6f8; padding: 0 .2rem; }}
</style></head><body>
{body}
</body></html>"""


def _state_class(value: str) -> str:
    if value in ("HEALTHY", "SUCCEEDED", "OK", "PASSED", "SUPPORTED"):
        return "ok"
    if value in ("DEGRADED", "SUCCEEDED_WITH_WARNINGS", "WARNING", "UNVERIFIED"):
        return "warn"
    return "bad"


def render_operations_view(session: Session, *, settings: AppConfig | None = None) -> str:
    """`GET /ui` — the operations view required by doc 04 §15."""
    from paperintel.services import read_models

    status = read_models.system_status(session, settings=settings)
    modules = read_models.modules_status()
    workers = read_models.workers_status(session, settings=settings)

    recent_failures = session.scalars(
        select(TaskRow)
        .where(TaskRow.state == TaskState.FAILED)
        .order_by(TaskRow.created_at.desc())
        .limit(10)
    ).all()

    redis_state = _redis_state(settings)
    db_state = _db_state(session)

    parts: list[str] = ["<h1>Akasha operations</h1>"]
    parts.append("<h2>System health</h2><table>")
    parts.append(
        f"<tr><th>overall</th><td class='{_state_class(status['overall_state'])}'>"
        f"{_esc(status['overall_state'])}</td></tr>"
    )
    parts.append(
        _row("spec / pipeline version", f"{status['spec_version']} / {status['pipeline_version']}")
    )
    parts.append(_row("papers", status["pipeline"]["papers"]))
    parts.append(
        _row(
            "claims (supported)",
            f"{status['pipeline']['claims']} ({status['pipeline']['claims_supported']})",
        )
    )
    parts.append(_row("evidence units", status["pipeline"]["evidence"]))
    parts.append(_row("jobs", status["pipeline"]["jobs"]))
    parts.append(_row("model calls", status["pipeline"]["model_calls"]))
    parts.append("</table>")

    parts.append("<h2>Queue</h2><table>")
    for state, count in status["queue"].items():
        parts.append(_row(state, count))
    parts.append("</table>")

    parts.append("<h2>Workers</h2><table>")
    parts.append(_row("mode", workers["mode"]))
    for task_type, states in workers["by_task_type"].items():
        parts.append(_row(task_type, ", ".join(f"{k}={v}" for k, v in sorted(states.items()))))
    parts.append("</table>")

    parts.append("<h2>Providers</h2><table>")
    if status["providers"]:
        for name, entry in status["providers"].items():
            parts.append(
                _row(
                    name,
                    f"{entry['family']} · breaker={entry['circuit_breaker_state']}",
                )
            )
    else:
        parts.append(_row("providers", "none configured"))
    parts.append("</table>")

    parts.append("<h2>Storage</h2><table>")
    disk = status["disk"]
    parts.append(
        f"<tr><th>disk level</th><td class='{_state_class(disk['level'])}'>"
        f"{_esc(disk['level'])} ({disk['free_percent']}% free)</td></tr>"
    )
    parts.append(_row("total bytes", disk["total_bytes"]))
    parts.append(_row("used bytes", disk["used_bytes"]))
    for name, value in sorted(disk["breakdown"].items()):
        parts.append(_row(f"breakdown: {name}", value))
    parts.append("</table>")

    parts.append("<h2>Dependencies</h2><table>")
    parts.append(
        f"<tr><th>database</th><td class='{_state_class(db_state)}'>{_esc(db_state)}</td></tr>"
    )
    parts.append(
        f"<tr><th>redis</th><td class='{_state_class(redis_state)}'>{_esc(redis_state)}</td></tr>"
    )
    parts.append("</table>")

    parts.append(f"<h2>Pipeline module health ({modules['count']} modules)</h2><table>")
    for module in modules["modules"]:
        parts.append(_row(module["module_id"], f"v{module['version']}"))
    parts.append("</table>")

    parts.append("<h2>Recent failures</h2><table>")
    if recent_failures:
        parts.append("<tr><th>task</th><th>type</th><th>error</th></tr>")
        for task in recent_failures:
            parts.append(
                f"<tr><td><code>{_esc(task.task_id)}</code></td>"
                f"<td>{_esc(task.task_type)}</td>"
                f"<td class='bad'>{_esc(task.error_code or '')}</td></tr>"
            )
    else:
        parts.append("<tr><td>none</td></tr>")
    parts.append("</table>")

    return _page("Akasha operations", "\n".join(parts))


def render_paper_view(session: Session, paper_id: str, *, settings: AppConfig | None = None) -> str:
    """`GET /ui/papers/{paper_id}` — the paper page required by doc 04 §15."""
    from paperintel.services import read_models

    context = read_models.paper_context(session, paper_id)
    analysis = read_models.paper_analysis(session, paper_id)
    audit = read_models.paper_audit(session, paper_id)
    pipeline = read_models.job_list(session, limit=200)
    paper_jobs = [job for job in pipeline["jobs"] if job["paper_id"] == paper_id]

    parts = [f"<h1>{_esc(context['paper']['canonical_title'])}</h1>"]
    parts.append(f"<p><code>{_esc(paper_id)}</code> · <a href='/ui'>operations view</a></p>")

    parts.append("<h2>Analysis health</h2><table>")
    by_state = context["claims"]["by_support_state"]
    supported = by_state.get(SupportState.SUPPORTED.value, 0)
    total = context["claims"]["total"] or 1
    health = "HEALTHY" if supported / total >= 0.5 else "DEGRADED"
    parts.append(
        f"<tr><th>claim health</th><td class='{_state_class(health)}'>{_esc(health)} "
        f"({supported}/{context['claims']['total']} supported)</td></tr>"
    )
    for state, count in sorted(by_state.items()):
        parts.append(_row(state, count))
    parts.append("</table>")

    parts.append("<h2>Pipeline stages</h2><table>")
    if paper_jobs:
        parts.append("<tr><th>job</th><th>state</th><th>stage</th><th>tasks</th></tr>")
        for job in paper_jobs:
            parts.append(
                f"<tr><td><code>{_esc(job['job_id'])}</code></td>"
                f"<td class='{_state_class(job['state'])}'>{_esc(job['state'])}</td>"
                f"<td>{_esc(job['current_stage'])}</td>"
                f"<td>{_esc(job['progress'])}</td></tr>"
            )
    else:
        parts.append("<tr><td>no jobs</td></tr>")
    parts.append("</table>")

    parts.append("<h2>Evidence quality</h2><table>")
    for state, count in sorted(context["evidence"]["by_quality_state"].items()):
        parts.append(_row(state, count))
    for method, count in sorted(context["evidence"]["by_source_method"].items()):
        parts.append(_row(f"source: {method}", count))
    parts.append("</table>")

    parts.append("<h2>Claims by state</h2><table>")
    parts.append("<tr><th>category</th><th>claims</th></tr>")
    for category, claims in sorted(analysis["by_category"].items()):
        parts.append(_row(category, len(claims)))
    parts.append("</table>")

    parts.append("<h2>High-risk audit items</h2><table>")
    parts.append(_row("incomplete audit bundles", len(audit.get("incomplete_bundles", []))))
    parts.append(_row("unverified claims", len(audit.get("unverified_claims", []))))
    for state in ("DISPUTED", "UNSUPPORTED"):
        parts.append(_row(state.lower(), audit.get("support_states", {}).get(state, 0)))
    parts.append("</table>")

    parts.append("<h2>Versions</h2><table>")
    parts.append(_row("spec version", context["versions"]["spec_version"]))
    parts.append(_row("pipeline version", context["versions"]["pipeline_version"]))
    models = {run["model_id"] for run in analysis["runs"]}
    parts.append(_row("models used", ", ".join(sorted(models)) or "none"))
    parts.append("</table>")

    return _page(f"Akasha · {context['paper']['canonical_title']}", "\n".join(parts))


def _redis_state(settings: AppConfig | None) -> str:
    """Redis reachability (a dependency check, never a hard failure)."""
    import socket
    from urllib.parse import urlparse

    if settings is None:
        from paperintel.config.settings import get_settings

        settings = get_settings()
    parsed = urlparse(settings.redis.url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return "CONNECTED"
    except OSError:
        return "UNREACHABLE"


def _db_state(session: Session) -> str:
    try:
        session.execute(select(func.count()).select_from(JobRow))
        return "CONNECTED"
    except Exception:  # noqa: BLE001 - dependency probe
        return "UNREACHABLE"


def evidence_quality_summary(session: Session, paper_version_id: str) -> dict[str, int]:
    """Evidence quality counts for one version (shared with the UI)."""
    rows = session.execute(
        select(EvidenceRow.quality_state, func.count())
        .where(EvidenceRow.paper_version_id == paper_version_id)
        .group_by(EvidenceRow.quality_state)
    ).all()
    return {
        (state.value if isinstance(state, DataQualityState) else str(state)): int(count)
        for state, count in rows
    }
