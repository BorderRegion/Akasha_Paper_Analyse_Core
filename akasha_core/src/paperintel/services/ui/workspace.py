"""services.ui.workspace — paper workspace, review queue and observations.

Assembles the read models the workbench pages need (docs/06 §文献库与工作台 /
§审查与外部Agent / §运维). It reuses the existing service layer; no new
business logic is invented here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimEvidenceRow,
    ClaimRow,
    CollectionPaperRow,
    PaperRow,
    PaperVersionRow,
    TriageResultRow,
)
from paperintel.errors import DomainError
from paperintel.schemas.enums import SupportState
from paperintel.schemas.ui.models import (
    AuditCounts,
    ClaimPreview,
    DiskObservation,
    FactValue,
    LibraryQuery,
    ModuleObservation,
    ModuleView,
    ObservedMetric,
    OperationSnapshot,
    PaperListItem,
    PersonalState,
    ProviderObservation,
    ReadingAnchor,
    ReviewItem,
    SourceRef,
    TagView,
    VersionSummary,
    WorkerObservation,
    Workspace,
)
from paperintel.services.ui import documents, library, personal

#: Which claim categories feed which workbench module (docs/03 §S04).
MODULE_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("overview", "概览", ("research.", "contribution.", "result.main", "limitation.")),
    ("method", "方法", ("method.",)),
    ("experiments", "实验", ("experiment.", "result.comparative", "reliability.")),
    ("audit", "核查", ("critique.", "reliability.", "result.superiority")),
)


def _claim_preview(claim: ClaimRow, *, evidence_count: int) -> ClaimPreview:
    return ClaimPreview(
        claim_id=claim.claim_id,
        statement=claim.statement,
        claim_type=claim.claim_type,
        support_state=claim.support_state,
        paper_version_id=claim.paper_version_id,
        evidence_count=evidence_count,
        is_superseded=claim.superseded_by_claim_id is not None,
    )


def _evidence_counts(session: Session, claim_ids: list[str]) -> dict[str, int]:
    if not claim_ids:
        return {}
    from paperintel.database.models import ClaimEvidenceRow

    rows = session.execute(
        select(ClaimEvidenceRow.claim_id, func.count())
        .where(ClaimEvidenceRow.claim_id.in_(claim_ids))
        .group_by(ClaimEvidenceRow.claim_id)
    ).all()
    return {claim_id: int(count) for claim_id, count in rows}


def _audit_counts(session: Session, version_id: str) -> AuditCounts:
    rows = session.execute(
        select(ClaimRow.support_state, func.count())
        .where(ClaimRow.paper_version_id == version_id)
        .group_by(ClaimRow.support_state)
    ).all()
    by_state = {
        (state.value if hasattr(state, "value") else str(state)): int(count)
        for state, count in rows
    }
    return AuditCounts(total=sum(by_state.values()), by_state=by_state)


def _personal(paper_id: str, session: Session) -> PersonalState:
    raw = personal.personal_state(session, paper_id)
    anchor = raw.get("reading_anchor")
    return PersonalState(
        saved=bool(raw["saved"]),
        read_state=raw["read_state"],
        revision=int(raw["revision"]),
        reading_anchor=ReadingAnchor(**anchor) if anchor else None,
    )


def _latest_version(session: Session, paper_id: str) -> PaperVersionRow:
    version = session.scalars(
        select(PaperVersionRow)
        .where(PaperVersionRow.paper_id == paper_id)
        .order_by(PaperVersionRow.created_at.desc(), PaperVersionRow.paper_version_id.desc())
    ).first()
    if version is None:
        raise DomainError(
            "CFG_002", message="Paper has no imported versions.", details={"paper_id": paper_id}
        )
    return version


def _version_summaries(session: Session, paper_id: str, data_dir) -> list[VersionSummary]:
    rows = session.scalars(
        select(PaperVersionRow)
        .where(PaperVersionRow.paper_id == paper_id)
        .order_by(PaperVersionRow.created_at)
    ).all()
    summaries: list[VersionSummary] = []
    for row in rows:
        available = False
        try:
            documents.document_info(session, row.paper_version_id, data_dir=data_dir)
            available = True
        except DomainError:
            available = False
        summaries.append(
            VersionSummary(
                paper_version_id=row.paper_version_id,
                version_label=row.version_label,
                document_sha256=row.content_sha256,
                document_available=available,
            )
        )
    return summaries


def paper_list_item(
    session: Session,
    paper: PaperRow,
    *,
    version: PaperVersionRow,
    tags: dict[str, list[dict]],
    tier: str | None,
    takeaway: ClaimRow | None,
    evidence_count: int,
) -> PaperListItem:
    authors = FactValue(value=None, missing_reason="NOT_REPORTED")
    year = FactValue(value=None, missing_reason="NOT_REPORTED")
    if version.publication_date is not None:
        year = FactValue(value=version.publication_date.year)
    venue = FactValue(value=None, missing_reason="NOT_EXTRACTED")
    from paperintel.knowledge.collections import get_collection  # noqa: F401  (kv parity)

    return PaperListItem(
        paper_id=paper.paper_id,
        paper_version_id=version.paper_version_id,
        title=paper.canonical_title,
        authors=authors,
        year=year,
        venue=venue,
        takeaway=_claim_preview(takeaway, evidence_count=evidence_count) if takeaway else None,
        tags=[TagView(**tag) for tag in tags.get(paper.paper_id, [])],
        tier=tier,
        personal=_personal(paper.paper_id, session),
        audit=_audit_counts(session, version.paper_version_id),
    )


def list_papers(
    session: Session, query: LibraryQuery, *, data_dir, default_page: int | None = None
) -> tuple[list[PaperListItem], library.LibraryPage]:
    """Query + hydrate one page of PaperListItem (batched joins, no N+1)."""
    page = library.query_library(session, query, default_page=default_page)
    if not page.paper_ids:
        return [], page

    papers = {
        row.paper_id: row
        for row in session.scalars(select(PaperRow).where(PaperRow.paper_id.in_(page.paper_ids)))
    }
    versions = {
        row.paper_id: row
        for row in session.scalars(
            select(PaperVersionRow)
            .where(PaperVersionRow.paper_id.in_(page.paper_ids))
            .order_by(PaperVersionRow.created_at.desc())
        )
    }
    tags = library.tags_for_papers(session, page.paper_ids)
    tiers = {
        row.paper_id: row.effective_tier.value
        for row in session.scalars(
            select(TriageResultRow).where(TriageResultRow.paper_id.in_(page.paper_ids))
        )
    }
    takeaways: dict[str, ClaimRow] = {}
    for claim in session.scalars(
        select(ClaimRow)
        .where(ClaimRow.paper_id.in_(page.paper_ids), ClaimRow.category == "result.main")
        .order_by(ClaimRow.created_at.desc())
    ):
        takeaways.setdefault(claim.paper_id, claim)
    counts = _evidence_counts(session, [claim.claim_id for claim in takeaways.values()])

    items: list[PaperListItem] = []
    for paper_id in page.paper_ids:
        paper = papers.get(paper_id)
        version = versions.get(paper_id)
        if paper is None or version is None:
            continue
        takeaway = takeaways.get(paper_id)
        items.append(
            paper_list_item(
                session,
                paper,
                version=version,
                tags=tags,
                tier=tiers.get(paper_id),
                takeaway=takeaway,
                evidence_count=counts.get(takeaway.claim_id, 0) if takeaway else 0,
            )
        )
    return items, page


def workspace(
    session: Session, paper_id: str, *, paper_version_id: str | None, data_dir
) -> Workspace:
    paper = session.get(PaperRow, paper_id)
    if paper is None:
        raise DomainError(
            "CFG_002", message=f"Unknown paper ID: {paper_id}", details={"paper_id": paper_id}
        )
    versions = _version_summaries(session, paper_id, data_dir)
    if not versions:
        raise DomainError(
            "CFG_002", message="Paper has no imported versions.", details={"paper_id": paper_id}
        )
    if paper_version_id is None:
        # The server picks ONCE and reports it; the client pins it afterwards so
        # evidence and comparisons never jump to a newer version (docs/06).
        selected = _latest_version(session, paper_id)
    else:
        selected = session.get(PaperVersionRow, paper_version_id)
        if selected is None or selected.paper_id != paper_id:
            raise DomainError(
                "CFG_002",
                message="paper_version_id does not belong to this paper.",
                details={"paper_id": paper_id, "paper_version_id": paper_version_id},
            )

    tags = library.tags_for_papers(session, [paper_id])
    tier_row = session.scalars(
        select(TriageResultRow).where(TriageResultRow.paper_id == paper_id)
    ).first()
    takeaway = session.scalars(
        select(ClaimRow)
        .where(
            ClaimRow.paper_version_id == selected.paper_version_id,
            ClaimRow.category == "result.main",
        )
        .order_by(ClaimRow.created_at.desc())
    ).first()
    counts = _evidence_counts(session, [takeaway.claim_id] if takeaway else [])
    item = paper_list_item(
        session,
        paper,
        version=selected,
        tags=tags,
        tier=tier_row.effective_tier.value if tier_row else None,
        takeaway=takeaway,
        evidence_count=counts.get(takeaway.claim_id, 0) if takeaway else 0,
    )

    claims = list(
        session.scalars(
            select(ClaimRow)
            .where(ClaimRow.paper_version_id == selected.paper_version_id)
            .order_by(ClaimRow.created_at, ClaimRow.claim_id)
        )
    )
    claim_counts = _evidence_counts(session, [claim.claim_id for claim in claims])
    modules: list[ModuleView] = []
    for module_id, label, prefixes in MODULE_CATEGORIES:
        matched = [claim for claim in claims if claim.category.startswith(prefixes)]
        modules.append(
            ModuleView(
                id=module_id,
                label=label,
                availability="AVAILABLE" if matched else "UNAVAILABLE",
                reason=None if matched else "该模块尚无已存分析（未运行或该阶段被策略跳过）",
                claims=[
                    _claim_preview(claim, evidence_count=claim_counts.get(claim.claim_id, 0))
                    for claim in matched
                ],
            )
        )

    runs = list(
        session.scalars(
            select(ClaimRow.created_by_run_id).where(
                ClaimRow.paper_version_id == selected.paper_version_id
            )
        )
    )
    analysis_revision = f"{selected.content_sha256[:12]}-{len(claims)}-{len(set(runs))}"
    return Workspace(
        paper=item,
        document_sha256=selected.content_sha256,
        selected_version_id=selected.paper_version_id,
        versions=versions,
        modules=modules,
        analysis_revision=analysis_revision,
        personal=_personal(paper_id, session),
        audit=_audit_counts(session, selected.paper_version_id),
    )


def review_queue(
    session: Session, *, limit: int = 50, collection_id: str | None = None
) -> list[ReviewItem]:
    """Claims worth a human look (docs/06 POST /v1/ui/review/query).

    The queue is built from real verification outcomes — disputed, unsupported,
    partially supported, supported-with-critique — never from a fabricated
    "risk score". Human decisions come from ui_review_decisions and are
    reported separately from support_state.
    """
    risky = (
        SupportState.DISPUTED,
        SupportState.UNSUPPORTED,
        SupportState.PARTIALLY_SUPPORTED,
        SupportState.INSUFFICIENT_EVIDENCE,
    )
    stmt = (
        select(ClaimRow)
        .where(ClaimRow.support_state.in_(risky))
        .order_by(ClaimRow.created_at.desc(), ClaimRow.claim_id)
        .limit(limit)
    )
    if collection_id:
        stmt = stmt.where(
            ClaimRow.paper_id.in_(
                select(CollectionPaperRow.paper_id).where(
                    CollectionPaperRow.collection_id == collection_id
                )
            )
        )
    claims = list(session.scalars(stmt))
    decisions = personal.latest_decisions(session)
    counts = _evidence_counts(session, [claim.claim_id for claim in claims])
    titles = {
        row.paper_id: row.canonical_title
        for row in session.scalars(
            select(PaperRow).where(PaperRow.paper_id.in_({claim.paper_id for claim in claims}))
        )
    }
    items: list[ReviewItem] = []
    for claim in claims:
        evidence_ids = list(
            session.scalars(
                select(ClaimEvidenceRow.evidence_id)
                .where(ClaimEvidenceRow.claim_id == claim.claim_id)
                .limit(5)
            )
        )
        reasons = [f"support_state={claim.support_state.value}"]
        if claim.claim_type.value == "CRITIQUE":
            reasons.append("批评性结论，需人工判断")
        impact = (
            "HIGH"
            if claim.support_state in (SupportState.DISPUTED, SupportState.UNSUPPORTED)
            else "NORMAL"
        )
        items.append(
            ReviewItem(
                claim=_claim_preview(claim, evidence_count=counts.get(claim.claim_id, 0)),
                paper_id=claim.paper_id,
                paper_title=titles.get(claim.paper_id, ""),
                reasons=reasons,
                impact=impact,
                source_refs=[
                    SourceRef(
                        paper_id=claim.paper_id,
                        paper_version_id=claim.paper_version_id,
                        claim_id=claim.claim_id,
                        evidence_ids=evidence_ids,
                    )
                ],
                personal_decision=decisions.get(claim.claim_id),
            )
        )
    return items


def operations_snapshot(session: Session, *, settings, runtime=None) -> OperationSnapshot:
    """Real observations with explicit freshness (docs/06 §运维).

    Nothing here is inferred from configuration counts: a provider with no
    samples reports UNKNOWN with a reason, and a module without a healthcheck
    result says so.
    """
    from paperintel.database.models import TaskRow
    from paperintel.operations.disk import disk_report
    from paperintel.schemas.enums import TaskState

    queue = {
        "pending": session.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.state.in_([TaskState.PENDING, TaskState.QUEUED, TaskState.WAITING]))
        )
        or 0,
        "running": session.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.state.in_([TaskState.RUNNING, TaskState.RETRYING]))
        )
        or 0,
        "failed": session.scalar(
            select(func.count()).select_from(TaskRow).where(TaskRow.state == TaskState.FAILED)
        )
        or 0,
    }
    disk = disk_report(settings.core.data_dir, settings=settings)
    now = datetime.now(UTC)

    from paperintel.operations.heartbeat import read_heartbeats

    unknown_reasons: list[str] = []
    heartbeats = read_heartbeats(settings.core.data_dir)
    workers: list[WorkerObservation] = [
        WorkerObservation(
            identity=beat.identity,
            last_seen=datetime.fromtimestamp(beat.last_seen, tz=UTC),
            state=f"{beat.state}（{int(beat.age_seconds)}s 前）",
        )
        for beat in heartbeats
    ]
    if not heartbeats:
        workers.append(WorkerObservation(identity="(none)", last_seen=None, state="UNOBSERVED"))
        unknown_reasons.append(
            "No worker heartbeat recorded yet; the online worker count is not inferred "
            "from running tasks."
        )

    modules: list[ModuleObservation] = []
    try:
        from paperintel.modules import load_bundled_manifests

        for module_id in sorted(load_bundled_manifests()):
            modules.append(
                ModuleObservation(
                    module_id=module_id,
                    health="UNKNOWN",
                    observed_at=None,
                    source="manifest",
                    reason="manifest declares a healthcheck; no recorded result in this snapshot",
                )
            )
    except Exception:  # noqa: BLE001 - manifests are optional at runtime
        unknown_reasons.append("Module manifests could not be loaded.")

    providers: list[ProviderObservation] = []
    try:
        providers = _provider_observations(settings)
    except Exception:  # noqa: BLE001 - observations are best-effort and reported
        unknown_reasons.append("Provider observations unavailable in this build.")

    return OperationSnapshot(
        queue=queue,
        workers=workers,
        modules=modules,
        providers=providers,
        disk=_disk_observations(disk, now, settings),
        eta_available=False,
        observed_at=now,
        unknown_reasons=unknown_reasons,
    )


def _disk_observations(disk: dict, now: datetime, settings) -> list[DiskObservation]:
    """One observation per mount point, with explicit overlap marking.

    The object store, the cache and the database are often on the SAME device:
    reporting three independent "free space" rows would let a reader add them up.
    Each row therefore names the device and the other stores it shares it with,
    and an unreadable directory is reported as not measurable instead of 0 B.
    """
    observations: list[DiskObservation] = []
    data_dir = Path(disk["data_dir"])
    device = _device_of(data_dir)
    if device is None:
        return [
            DiskObservation(
                mount_id="data",
                free_bytes=None,
                total_bytes=None,
                application_bytes=None,
                observed_at=now,
                level="UNKNOWN",
                measurable=False,
                reason="数据目录不可读取，无法测量（不是 0 字节）",
            )
        ]
    # Every store under core.data_dir is on this mount BY CONSTRUCTION, so it is
    # always listed: the overlap is a fact about the layout, not about whether the
    # directory has been created yet.
    same_device: list[str] = ["objects", "cache", "exports", "debug", "temp"]
    db_url = settings.database.url or ""
    if "postgresql" in db_url:
        # The database lives wherever the server put it; if its data directory is
        # on this device the report says so rather than assuming.
        db_data = Path("/var/lib/postgresql")
        if db_data.exists() and _device_of(db_data) == device:
            same_device.append("database")
    observations.append(
        DiskObservation(
            mount_id="data",
            free_bytes=disk["disk"]["free_bytes"],
            total_bytes=disk["disk"]["total_bytes"],
            application_bytes=disk["store_bytes"],
            observed_at=now,
            level=disk["disk"]["level"],
            measurable=True,
            device=device,
            shares_mount_with=sorted(same_device),
            reason=(
                "对象/缓存/导出与数据库在同一挂载点，容量数字会重叠，不能相加"
                if "database" in same_device
                else None
            ),
        )
    )
    return observations


def _device_of(path: Path) -> str | None:
    """The device id of the mount holding ``path`` (None when unreadable)."""
    import os

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return f"{os.stat(probe).st_dev:#x}"
    except OSError:
        return None


def _observed_metric(
    *,
    value: float | None,
    unit: str,
    sample_count: int,
    window_seconds: int | None,
    observed_at: datetime | None,
    reason: str | None,
) -> ObservedMetric:
    """Wrap a measurement with its freshness — never a bare number."""
    if value is None or sample_count == 0:
        return ObservedMetric(
            value=None,
            unit=unit,
            state="UNKNOWN",
            observed_at=None,
            sample_count=sample_count,
            window_seconds=window_seconds,
            reason=reason or "NOT_MEASURED",
        )
    age = (datetime.now(UTC) - observed_at).total_seconds() if observed_at else None
    state = "FRESH"
    if age is None or age > 300:
        state = "STALE"
    return ObservedMetric(
        value=value,
        unit=unit,
        state=state,
        observed_at=observed_at,
        sample_count=sample_count,
        window_seconds=window_seconds,
        reason=reason,
    )


def _provider_observations(settings) -> list[ProviderObservation]:
    """Provider telemetry from DURABLE events (shared by API/worker), never
    from a freshly rebuilt registry's in-process counters."""
    from paperintel.operations.provider_observations import snapshots
    from paperintel.services.read_models import providers_status

    observed = snapshots(settings.core.data_dir)
    configured = providers_status(settings=settings).get("providers", {})
    entries: list[ProviderObservation] = []
    for name, state in observed.items():
        attempts = int(state.get("total_calls") or 0)
        observed_at = datetime.now(UTC) if attempts else None
        entries.append(
            ProviderObservation(
                id=name,
                configured_limit=configured.get(name, {}).get("configured_limit"),
                observed_active=_observed_metric(
                    value=float(attempts) if attempts else None,
                    unit="calls",
                    sample_count=attempts,
                    window_seconds=None,
                    observed_at=observed_at,
                    reason=None if attempts else "NOT_MEASURED",
                ),
                latency_p50=_observed_metric(
                    value=state.get("latency_p50"),
                    unit="ms",
                    sample_count=attempts,
                    window_seconds=None,
                    observed_at=observed_at,
                    reason=None if attempts else "NOT_MEASURED",
                ),
                latency_p90=_observed_metric(
                    value=state.get("latency_p90"),
                    unit="ms",
                    sample_count=attempts,
                    window_seconds=None,
                    observed_at=observed_at,
                    reason=None if attempts else "NOT_MEASURED",
                ),
                error_rate=_observed_metric(
                    value=state.get("transport_error_rate"),
                    unit="ratio",
                    sample_count=attempts,
                    window_seconds=None,
                    observed_at=observed_at,
                    reason=None if attempts else "NOT_MEASURED",
                ),
            )
        )
    for name in sorted(set(configured) - set(observed)):
        entries.append(
            ProviderObservation(
                id=name,
                configured_limit=configured[name].get("configured_limit"),
                observed_active=_observed_metric(
                    value=None,
                    unit="calls",
                    sample_count=0,
                    window_seconds=None,
                    observed_at=None,
                    reason="NO_OBSERVATIONS",
                ),
                latency_p50=_observed_metric(
                    value=None,
                    unit="ms",
                    sample_count=0,
                    window_seconds=None,
                    observed_at=None,
                    reason="NOT_MEASURED",
                ),
                latency_p90=_observed_metric(
                    value=None,
                    unit="ms",
                    sample_count=0,
                    window_seconds=None,
                    observed_at=None,
                    reason="NOT_MEASURED",
                ),
                error_rate=_observed_metric(
                    value=None,
                    unit="ratio",
                    sample_count=0,
                    window_seconds=None,
                    observed_at=None,
                    reason="NOT_MEASURED",
                ),
            )
        )
    return entries


__all__ = [
    "MODULE_CATEGORIES",
    "list_papers",
    "operations_snapshot",
    "paper_list_item",
    "review_queue",
    "workspace",
]
