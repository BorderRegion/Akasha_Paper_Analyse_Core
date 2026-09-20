"""``paperctl`` — PaperIntel operations CLI (spec doc 04 §8).

Implemented so far:
- ``paperctl version``                                   (P00)
- ``paperctl config validate``                           (P00)
- ``paperctl doctor``                                    (P01/P02 scope)
- ``paperctl db status``                                 (P01)
- ``paperctl providers status`` / ``providers canary``   (P02)
- ``paperctl import``                                    (P03)

Later phases add: status (P05/P12), selftest (P12), inspect (P04),
audit (P08), jobs/rerun/replay/trace (P05), debug-bundle (P12), disk/gc
(P10). Stubs for not-yet-implemented commands fail loudly
with the phase that delivers them — never silently (spec doc 06 §16).
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

import typer

from paperintel import __version__
from paperintel.config.provider_config import (
    load_provider_config,
    provider_config_fingerprint,
)
from paperintel.config.settings import AppConfig, load_config
from paperintel.errors import DomainError
from paperintel.version import PIPELINE_VERSION, SPEC_VERSION

app = typer.Typer(
    name="paperctl",
    help="Akasha operations CLI (spec 1.0.0).",
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

config_app = typer.Typer(help="Configuration commands.")
app.add_typer(config_app, name="config")

providers_app = typer.Typer(help="Provider commands.")
app.add_typer(providers_app, name="providers")

#: Commands whose functionality arrives in a later phase (honest stubs).
_PHASE_GATE: dict[str, str] = {
    "status": "P05 (jobs) with the operations view at P12",
    "selftest": "P12 (end-to-end golden pipeline)",
    "audit": "P08 (verification/audit engine)",
    "jobs": "P05 (workflow engine)",
    "rerun": "P05 (workflow engine)",
    "replay": "P05 (workflow engine)",
    "trace": "P05 (workflow engine)",
    "debug-bundle": "P12 (operations surfaces)",
}

CURRENT_BUILD_PHASE = "P12"


def _emit(payload: dict[str, Any], *, ok: bool = True) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    if not ok:
        raise typer.Exit(code=1)


def _fail_domain_error(exc: DomainError) -> None:
    typer.echo(json.dumps(exc.to_envelope(), indent=2, ensure_ascii=False, default=str), err=True)
    raise typer.Exit(code=1)


def _phase_stub(command: str) -> None:
    phase = _PHASE_GATE.get(command, "a later phase")
    typer.echo(
        json.dumps(
            {
                "error": {
                    "code": "INTERNAL_001",
                    "message": (
                        f"paperctl {command} is not implemented yet; it is delivered by "
                        f"phase {phase}. Current build phase: {CURRENT_BUILD_PHASE}."
                    ),
                    "retryable": False,
                    "severity": "WARNING",
                    "trace_id": None,
                    "details": {"command": command, "delivered_by_phase": phase},
                }
            },
            indent=2,
        ),
        err=True,
    )
    raise typer.Exit(code=2)


@app.command()
def version() -> None:
    """Print program and specification versions."""
    _emit(
        {
            "program": "paperctl",
            "version": __version__,
            "spec_version": SPEC_VERSION,
            "pipeline_version": PIPELINE_VERSION,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
    )


@config_app.command("validate")
def config_validate(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to paperintel YAML config (default: $PAPERINTEL_CONFIG)."
    ),
    providers: Path | None = typer.Option(
        None,
        "--providers",
        "-p",
        help="Path to providers.yaml (default: providers_file from config, if set).",
    ),
) -> None:
    """Validate configuration and (when present) provider configuration.

    Secrets are referenced by environment variable name only; values are never
    printed. Exit code 0 = valid, 1 = invalid (envelope on stderr).
    """
    try:
        settings = load_config(config)
        providers_path = providers or settings.providers_file
        result: dict[str, Any] = {
            "config": "VALID",
            "source": str(config) if config else "defaults+env",
            "core": {
                "env": settings.core.env,
                "data_dir": str(settings.core.data_dir.resolve()),
                "log_level": settings.core.log_level,
            },
            "database_url_configured": bool(settings.database.url),
            "redis_url_configured": bool(settings.redis.url),
            "api_token_configured": settings.api.token is not None,
        }
        if providers_path is not None:
            provider_config = load_provider_config(providers_path)
            result["providers"] = {
                "source": str(providers_path),
                "spec_version": provider_config.spec_version,
                "entries": {
                    name: {
                        "kind": entry.kind.value,
                        "api_key_env": entry.api_key_env,
                        "api_key_present_in_environment": (
                            entry.api_key_env is not None
                            and bool(__import__("os").environ.get(entry.api_key_env))
                        ),
                        "models": sorted(entry.models),
                    }
                    for name, entry in sorted(provider_config.providers.items())
                },
                "fingerprint_sha256": provider_config_fingerprint(provider_config),
            }
        else:
            result["providers"] = "NOT_CONFIGURED"
        _emit(result)
    except DomainError as exc:
        _fail_domain_error(exc)


def _resolve_provider_config(providers: Path | None, settings: AppConfig) -> tuple[Any, str]:
    """Load the providers config from --providers or settings.providers_file.

    Raises DomainError CFG_001 when neither is configured.
    """
    path = providers or settings.providers_file
    if path is None:
        raise DomainError(
            "CFG_001",
            message=(
                "No providers configuration: pass --providers or set "
                "core.providers_file in the application config."
            ),
            details={},
        )
    return load_provider_config(Path(path)), str(path)


def _provider_state_json(provider: Any) -> dict[str, Any]:
    """Serialize a provider's status/health without any secret material."""
    if hasattr(provider, "status"):
        status = provider.status()
        payload = json.loads(status.model_dump_json())
        payload["kind"] = "status"
        return payload
    record = asyncio.run(provider.health())
    payload = json.loads(record.model_dump_json())
    payload["kind"] = "health"
    return payload


@providers_app.command("status")
def providers_status(
    providers: Path | None = typer.Option(
        None, "--providers", "-p", help="Path to providers.yaml."
    ),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to paperintel YAML config."
    ),
) -> None:
    """Show configured provider status (doc 04 §6 exposures).

    Offline: reports configuration, circuit-breaker state, and accumulated
    statistics — no active network calls (use ``providers canary`` for those).
    Secret values are never printed.
    """
    try:
        settings = load_config(config)
        provider_config, source = _resolve_provider_config(providers, settings)
        from paperintel.providers.factory import build_registry  # noqa: PLC0415

        registry = build_registry(
            provider_config, env=dict(os.environ), data_dir=settings.core.data_dir
        )
        result: dict[str, Any] = {
            "source": source,
            "spec_version": provider_config.spec_version,
            "fingerprint_sha256": provider_config_fingerprint(provider_config),
            "providers": {},
        }
        for name in registry.names():
            provider = registry.get(name)
            entry = {
                "family": provider.family.value,
                "provider_id": provider.provider_id,
            }
            entry.update(_provider_state_json(provider))
            result["providers"][name] = entry
        _emit(result)
    except DomainError as exc:
        _fail_domain_error(exc)


@providers_app.command("canary")
def providers_canary(
    providers: Path | None = typer.Option(
        None, "--providers", "-p", help="Path to providers.yaml."
    ),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to paperintel YAML config."
    ),
    provider: str | None = typer.Option(
        None, "--provider", help="Run the canary for a single configured provider name."
    ),
) -> None:
    """Run ACTIVE provider canaries (doc 04 §6/§10).

    Requires working credentials/network for real providers; mocks run
    offline. Exit 0 when every selected canary is OK, else 1.
    """
    try:
        settings = load_config(config)
        provider_config, source = _resolve_provider_config(providers, settings)
        from paperintel.providers.factory import build_registry  # noqa: PLC0415

        registry = build_registry(
            provider_config, env=dict(os.environ), data_dir=settings.core.data_dir
        )
        names = [provider] if provider else registry.names()
        if provider and provider not in registry.names():
            raise DomainError(
                "CFG_002",
                message=f"Provider {provider!r} is not configured.",
                details={"configured": registry.names()},
            )
        results: dict[str, Any] = {}
        all_ok = True
        ran_any = False
        for name in names:
            target = registry.get(name)
            if not hasattr(target, "run_canary"):
                results[name] = {"canary_state": "UNSUPPORTED"}
                continue
            record = asyncio.run(target.run_canary())
            ran_any = True
            results[name] = {
                "canary_state": record.state.value,
                "detail": record.detail,
                "latency_ms": record.latency_ms,
            }
            if record.state.value != "OK":
                all_ok = False
        if not ran_any:
            raise DomainError(
                "PROVIDER_001",
                message="No canary-capable provider selected.",
                details={"providers": names},
            )
        _emit({"source": source, "canaries": results}, ok=all_ok)
    except DomainError as exc:
        _fail_domain_error(exc)


@app.command()
def doctor(
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to paperintel YAML config."
    ),
) -> None:
    """System doctor covering deployment prerequisites. Exit 0 = healthy (warnings allowed),
    1 = problems found. DEGRADED states are warnings, not problems;
    FAILED/UNAVAILABLE/MISCONFIGURED are problems."""
    problems: list[str] = []
    warnings: list[str] = []
    report: dict[str, Any] = {"checks": {}}
    try:
        settings = load_config(config)
        report["checks"]["config"] = "PASS"
    except DomainError as exc:
        report["checks"]["config"] = f"FAIL ({exc.code})"
        problems.append("config")
        _emit(report, ok=False)
        return

    # Database
    try:
        from paperintel.database.base import create_engine_from_settings  # noqa: PLC0415
        from paperintel.database.health import database_health  # noqa: PLC0415

        engine = create_engine_from_settings(settings)
        try:
            record = database_health(engine)
        finally:
            engine.dispose()
        report["checks"]["database"] = record.state.value
        report["database"] = json.loads(record.model_dump_json())
        if record.state.value == "DEGRADED":
            warnings.append("database")
        elif record.state.value != "HEALTHY":
            problems.append("database")
    except DomainError as exc:
        report["checks"]["database"] = f"FAIL ({exc.code})"
        problems.append("database")

    # Redis
    try:
        import redis as redis_lib  # noqa: PLC0415

        client = redis_lib.Redis.from_url(settings.redis.url, socket_connect_timeout=3)
        pong = client.ping()
        client.close()
        report["checks"]["redis"] = "PASS" if pong else "FAIL"
        if not pong:
            problems.append("redis")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        report["checks"]["redis"] = f"FAIL ({type(exc).__name__})"
        problems.append("redis")

    # Object store + disk
    try:
        from paperintel.storage.object_store import LocalObjectStore  # noqa: PLC0415

        data_dir = Path(settings.core.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        store = LocalObjectStore(
            data_dir / "objects",
            warning_free_percent=settings.disk.warning_free_percent,
            critical_free_percent=settings.disk.critical_free_percent,
        )
        record = asyncio.run(store.health())
        report["checks"]["object_store"] = record.state.value
        report["object_store"] = json.loads(record.model_dump_json())
        if record.state.value == "DEGRADED":
            warnings.append("object_store")
        elif record.state.value != "HEALTHY":
            problems.append("object_store")
        free_percent = shutil.disk_usage(data_dir).free / shutil.disk_usage(data_dir).total * 100
        report["checks"]["disk"] = (
            "PASS"
            if free_percent >= settings.disk.warning_free_percent
            else ("WARN" if free_percent >= settings.disk.critical_free_percent else "FAIL")
        )
        report["disk_free_percent"] = round(free_percent, 2)
        if report["checks"]["disk"] == "FAIL":
            problems.append("disk")
        elif report["checks"]["disk"] == "WARN":
            warnings.append("disk")
    except DomainError as exc:
        report["checks"]["object_store"] = f"FAIL ({exc.code})"
        problems.append("object_store")

    from paperintel.operations.doctor import additional_checks

    extra = additional_checks(settings)
    report["checks"].update(extra)
    problems.extend(name for name, result in extra.items() if result.startswith("FAIL"))
    warnings.extend(name for name, result in extra.items() if result.startswith("UNKNOWN"))
    report["problems"] = problems
    report["warnings"] = warnings
    _emit(report, ok=not problems)


@app.command()
def db(
    subcommand: str = typer.Argument("status", help="db subcommand (status)."),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Path to paperintel YAML config."
    ),
) -> None:
    """Database utilities: ``db status`` reports connectivity, alembic
    revision vs head, and pgvector presence."""
    if subcommand != "status":
        typer.echo(
            json.dumps(
                {
                    "error": {
                        "code": "CFG_002",
                        "message": f"Unknown db subcommand {subcommand!r}; supported: status.",
                        "retryable": False,
                        "severity": "WARNING",
                        "trace_id": None,
                        "details": {"subcommand": subcommand},
                    }
                },
                indent=2,
            ),
            err=True,
        )
        raise typer.Exit(code=2)
    try:
        from paperintel.database.base import create_engine_from_settings  # noqa: PLC0415
        from paperintel.database.health import database_health, head_revision  # noqa: PLC0415

        settings = load_config(config)
        engine = create_engine_from_settings(settings)
        try:
            record = database_health(engine)
        finally:
            engine.dispose()
        payload = json.loads(record.model_dump_json())
        payload["head_revision"] = head_revision()
        _emit(payload, ok=record.state.value == "HEALTHY")
    except DomainError as exc:
        _fail_domain_error(exc)


@app.command()
def status(
    watch: bool = typer.Option(False, "--watch", help="Refresh continuously."),
    interval: float = typer.Option(2.0, "--interval", help="Refresh seconds (--watch)."),
) -> None:
    """System status overview (P12): queue, providers, disk, pipeline."""
    import time  # noqa: PLC0415

    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )
    from paperintel.services import read_models  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        while True:
            with session_scope(factory) as session:
                payload = read_models.system_status(session, settings=settings)
            _emit(payload, ok=payload["overall_state"] != "CRITICAL")
            if not watch:
                return
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return
    finally:
        engine.dispose()


@app.command()
def selftest(
    mock: bool = typer.Option(False, "--mock", help="Deterministic mock E2E."),
    real_providers: bool = typer.Option(False, "--real-providers", help="Real provider canaries."),
    keep_artifacts: bool = typer.Option(
        False, "--keep-artifacts", help="Do not clean test objects."
    ),
    with_analysis: bool = typer.Option(
        True, "--analysis/--no-analysis", help="Run the full analysis pipeline."
    ),
) -> None:
    """End-to-end selftest: import → extract → structure → evidence →
    triage → agents → verification → synthesis → graph → search, on a
    generated fixture, with the configured (mock) providers (P12)."""
    from paperintel.operations.selftest import run_selftest  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    if not mock and not real_providers:
        _fail_domain_error(
            DomainError(
                "CFG_002",
                message="Choose --mock or --real-providers (no implicit mode).",
                details={},
            )
        )
        return
    try:
        payload = run_selftest(
            settings,
            mock=mock,
            real_providers=real_providers,
            keep_artifacts=keep_artifacts,
            with_analysis=with_analysis,
        )
    except DomainError as exc:
        _fail_domain_error(exc)
        return
    _emit(payload, ok=payload["status"] == "PASS")


# ---------------------------------------------------------------------------
# Honest stubs for the remaining doc 04 §8 command surface. Each names the
# phase that delivers it and exits 2 (never a silent no-op).
# ---------------------------------------------------------------------------


@app.command("import")
def import_cmd(
    paths: list[str] = typer.Argument(help="PDF file(s) or directory to import."),
    providers: Path | None = typer.Option(
        None, "--providers", "-p", help="providers.yaml supplying the OCR fallback."
    ),
    ocr_provider_name: str | None = typer.Option(
        None, "--ocr-provider", help="OCR provider name (default: first OCR-family entry)."
    ),
    no_ocr: bool = typer.Option(
        False, "--no-ocr", help="Disable OCR fallback (pages needing it are flagged)."
    ),
    config: Path | None = typer.Option(None, "--config", "-c", help="Application config."),
    title: str | None = typer.Option(None, "--title", help="Explicit title (single file only)."),
    doi: str | None = typer.Option(None, "--doi", help="Explicit DOI (single file only)."),
    version_label: str | None = typer.Option(
        None, "--version-label", help="Explicit version label (single file only)."
    ),
    source_type: str = typer.Option("local_file", "--source-type", help="Version source type."),
) -> None:
    """Import PDFs: fingerprint, dedup, inspect, extract, report (P03).

    Without a providers config the import still runs; pages classified as
    needing OCR are honestly flagged OCR_UNAVAILABLE in the extraction
    report (never silently skipped). Per-file results are emitted as JSON;
    a failed file never rolls back successfully imported ones.
    """
    try:
        settings = load_config(config)
    except DomainError as exc:
        _fail_domain_error(exc)
        return
    if not paths:
        _fail_domain_error(DomainError("CFG_002", message="No input paths given.", details={}))
        return

    files: list[Path] = []
    for raw in paths:
        candidate = Path(raw)
        if candidate.is_dir():
            files.extend(sorted(candidate.glob("*.pdf")))
        else:
            files.append(candidate)
    if not files:
        _emit({"imported": [], "failed": [], "ok": True, "note": "no PDF files found"})
        return
    single = len(files) == 1
    if (title or doi or version_label) and not single:
        _fail_domain_error(
            DomainError(
                "CFG_002",
                message="--title/--doi/--version-label require exactly one input file.",
                details={"files": len(files)},
            )
        )
        return

    # OCR provider resolution (optional but LOUD when explicitly requested).
    ocr: Any = None
    registry: Any = None
    if not no_ocr:
        try:
            if ocr_provider_name or providers is not None:
                provider_config, _source = _resolve_provider_config(providers, settings)
                from paperintel.providers.factory import build_registry  # noqa: PLC0415

                registry = build_registry(
                    provider_config,
                    env=dict(os.environ),
                    data_dir=settings.core.data_dir,
                )
                if ocr_provider_name:
                    ocr = registry.get_ocr(ocr_provider_name)
                else:
                    from paperintel.schemas.enums import ProviderFamily  # noqa: PLC0415

                    family = registry.by_family(ProviderFamily.OCR)
                    if family:
                        ocr = registry.get_ocr(sorted(family)[0])
            elif settings.providers_file is not None:
                provider_config, _source = _resolve_provider_config(None, settings)
                from paperintel.providers.factory import build_registry  # noqa: PLC0415
                from paperintel.schemas.enums import ProviderFamily  # noqa: PLC0415

                registry = build_registry(
                    provider_config,
                    env=dict(os.environ),
                    data_dir=settings.core.data_dir,
                )
                family = registry.by_family(ProviderFamily.OCR)
                if family:
                    ocr = registry.get_ocr(sorted(family)[0])
        except DomainError as exc:
            _fail_domain_error(exc)
            return

    async def _run_all() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        from paperintel.database.base import (  # noqa: PLC0415
            create_engine_from_settings,
            create_session_factory,
            session_scope,
        )
        from paperintel.ingest.service import import_pdf  # noqa: PLC0415
        from paperintel.storage.object_store import LocalObjectStore  # noqa: PLC0415

        engine = create_engine_from_settings(settings)
        factory = create_session_factory(engine)
        data_dir = Path(settings.core.data_dir)
        store = LocalObjectStore(
            data_dir / "objects",
            warning_free_percent=settings.disk.warning_free_percent,
            critical_free_percent=settings.disk.critical_free_percent,
        )
        imported: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        try:
            for file in files:
                try:
                    with session_scope(factory) as session:
                        result = await import_pdf(
                            file,
                            session=session,
                            store=store,
                            data_dir=data_dir,
                            ocr_provider=ocr,
                            source_type=source_type,
                            version_label=version_label if single else None,
                            title=title if single else None,
                            doi=doi if single else None,
                        )
                    imported.append(result.cli_summary())
                except DomainError as exc:
                    failed.append({"file": file.name, "error": exc.to_envelope()["error"]})
        finally:
            engine.dispose()
            if ocr is not None:
                aclose = getattr(ocr, "aclose", None)
                if aclose is not None:
                    await aclose()
        return imported, failed

    imported, failed = asyncio.run(_run_all())
    _emit({"imported": imported, "failed": failed, "ok": not failed}, ok=not failed)


@app.command()
def inspect(
    paper_id: str = typer.Argument(help="Paper ID (pap_...)."),
    version: str | None = typer.Option(
        None, "--version", help="Version label (v3) or pver_ id; default: latest."
    ),
    evidence: str | None = typer.Option(
        None, "--evidence", help="Resolve one evidence ID (ev_...) and exit."
    ),
    page: int | None = typer.Option(None, "--page", help="List evidence for one page."),
    section: str | None = typer.Option(None, "--section", help="List one section's evidence."),
    limit: int = typer.Option(
        20, "--limit", help="Max evidence entries to list (0 = summary only)."
    ),
) -> None:
    """Paper/evidence snapshot (P04): versions, section tree, evidence counts.

    Later-phase fields (claims, verifications, search/graph readiness) are
    reported as pending with the phase that owns them — never omitted.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.database.models import PaperRow, PaperVersionRow  # noqa: PLC0415
    from paperintel.evidence import retrieval  # noqa: PLC0415
    from paperintel.schemas.structure import SectionNode  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    from paperintel.database.base import session_scope  # noqa: PLC0415

    try:
        with session_scope(factory) as session:
            paper = session.get(PaperRow, paper_id)
            if paper is None:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message=f"Unknown paper ID: {paper_id}",
                        details={"paper_id": paper_id},
                    )
                )
                return
            versions = session.scalars(
                select(PaperVersionRow)
                .where(PaperVersionRow.paper_id == paper.paper_id)
                .order_by(PaperVersionRow.created_at)
            ).all()
            if version:
                chosen = next(
                    (
                        v
                        for v in versions
                        if v.paper_version_id == version or v.version_label == version
                    ),
                    None,
                )
                if chosen is None:
                    _fail_domain_error(
                        DomainError(
                            "CFG_002",
                            message=f"Unknown version {version!r} for paper {paper_id}.",
                            details={"paper_id": paper_id, "version": version},
                        )
                    )
                    return
            else:
                chosen = versions[-1] if versions else None

            payload: dict[str, Any] = {
                "paper": {
                    "paper_id": paper.paper_id,
                    "canonical_title": paper.canonical_title,
                    "doi": paper.doi,
                    "created_at": paper.created_at.isoformat(),
                },
                "versions": [
                    {
                        "paper_version_id": v.paper_version_id,
                        "version_label": v.version_label,
                        "page_count": v.page_count,
                        "content_sha256": v.content_sha256,
                    }
                    for v in versions
                ],
            }
            if chosen is not None:
                tree: list[SectionNode] = retrieval.section_tree(session, chosen.paper_version_id)
                summary = retrieval.version_summary(session, chosen)
                payload["version"] = {
                    "paper_version_id": chosen.paper_version_id,
                    "version_label": chosen.version_label,
                    "page_count": chosen.page_count,
                    "content_sha256": chosen.content_sha256,
                }
                payload["sections"] = [node.model_dump() for node in tree]
                payload["section_counts"] = {
                    "total": sum(_count_nodes(n) for n in tree),
                    "top_level": len(tree),
                    "by_class": _classes_by_count(tree),
                }
                payload["evidence"] = summary
                rows = retrieval.list_evidence(
                    session,
                    chosen.paper_version_id,
                    pages={page} if page else None,
                    section_id=section,
                )
                if evidence:
                    row = retrieval.get_evidence(session, evidence)
                    payload["evidence_entry"] = {
                        "evidence_id": row.evidence_id,
                        "evidence_type": row.evidence_type.value,
                        "page_start": row.page_start,
                        "bbox": row.bbox,
                        "section_id": row.section_id,
                        "source_method": row.source_method.value,
                        "ocr_confidence": row.ocr_confidence,
                        "quality_state": row.quality_state.value,
                        "text": row.text,
                        "supersedes_evidence_id": row.supersedes_evidence_id,
                    }
                elif limit:
                    payload["evidence_entries"] = [
                        {
                            "evidence_id": row.evidence_id,
                            "evidence_type": row.evidence_type.value,
                            "page": row.page_start,
                            "text": (row.text or "")[:160],
                        }
                        for row in rows[:limit]
                    ]
                    if len(rows) > limit:
                        payload["evidence_entries_truncated"] = len(rows) - limit
            else:
                payload["version"] = None
                payload["note"] = "no versions imported for this paper"
            # Later-phase fields stay visible (honest pending state).
            payload["pending"] = {
                "claims": "P07",
                "verifications": "P07",
                "search_index": "P09",
                "graph": "P10",
                "pipeline_jobs": "P05",
            }
            payload["pipeline_version"] = PIPELINE_VERSION
        _emit(payload)
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command()
def paper(
    paper_id: str = typer.Argument(help="Paper ID (pap_...)."),
    context: bool = typer.Option(False, "--context", help="External-AI context view."),
    analysis: bool = typer.Option(False, "--analysis", help="Claims by category."),
) -> None:
    """Paper summary, context or analysis (P12, doc 04 §8)."""
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )
    from paperintel.services import read_models  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            if context:
                payload = read_models.paper_context(session, paper_id)
            elif analysis:
                payload = read_models.paper_analysis(session, paper_id)
            else:
                payload = read_models.paper_summary(session, paper_id)
    except DomainError as exc:
        _fail_domain_error(exc)
        return
    finally:
        engine.dispose()
    _emit(payload)


@app.command()
def audit(
    paper_id: str = typer.Argument(help="Paper ID (pap_...)."),
) -> None:
    """Audit bundle for one paper (P08): support states, unverified claims,
    incomplete provenance chains, model/pipeline versions."""
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )
    from paperintel.services import read_models  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            payload = read_models.paper_audit(session, paper_id)
    except DomainError as exc:
        _fail_domain_error(exc)
        return
    finally:
        engine.dispose()
    _emit(payload)


@app.command()
def jobs(
    state: str | None = typer.Option(None, "--state", help="Filter by job state."),
    limit: int = typer.Option(50, "--limit", help="Maximum jobs to list."),
) -> None:
    """List analysis jobs with per-state task counts (P05)."""
    from sqlalchemy import select  # noqa: PLC0415

    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.database.models import JobRow, TaskRow  # noqa: PLC0415
    from paperintel.schemas.enums import TaskState  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
            if state:
                try:
                    wanted = TaskState(state.upper())
                except ValueError:
                    _fail_domain_error(
                        DomainError(
                            "CFG_002",
                            message=f"Unknown job state: {state!r}",
                            details={"state": state},
                        )
                    )
                    return
                stmt = stmt.where(JobRow.state == wanted)
            jobs = session.scalars(stmt).all()
            payload = []
            for job in jobs:
                tasks = session.scalars(select(TaskRow).where(TaskRow.job_id == job.job_id)).all()
                by_state: dict[str, int] = {}
                for task in tasks:
                    by_state[task.state.value] = by_state.get(task.state.value, 0) + 1
                payload.append(
                    {
                        "job_id": job.job_id,
                        "paper_id": job.paper_id,
                        "paper_version_id": job.paper_version_id,
                        "state": job.state.value,
                        "current_stage": job.current_stage.value,
                        "effective_tier": job.effective_tier.value,
                        "trace_id": job.trace_id,
                        "tasks_total": len(tasks),
                        "tasks_by_state": dict(sorted(by_state.items())),
                        "created_at": job.created_at.isoformat(),
                    }
                )
        _emit({"jobs": payload, "count": len(payload)})
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command()
def job(
    job_id: str = typer.Argument(help="Job ID (job_...)."),
) -> None:
    """Show one job with its tasks and checkpoints (P05)."""
    from sqlalchemy import select  # noqa: PLC0415

    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.database.models import JobRow, TaskRow  # noqa: PLC0415
    from paperintel.workflow.engine import job_progress  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            row = session.get(JobRow, job_id)
            if row is None:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message=f"Unknown job ID: {job_id}",
                        details={"job_id": job_id},
                    )
                )
                return
            tasks = session.scalars(
                select(TaskRow).where(TaskRow.job_id == job_id).order_by(TaskRow.created_at)
            ).all()
            payload = {
                "job": {
                    "job_id": row.job_id,
                    "paper_id": row.paper_id,
                    "paper_version_id": row.paper_version_id,
                    "state": row.state.value,
                    "current_stage": row.current_stage.value,
                    "requested_tier": row.requested_tier.value,
                    "effective_tier": row.effective_tier.value,
                    "priority": row.priority,
                    "trace_id": row.trace_id,
                    "created_at": row.created_at.isoformat(),
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                },
                "progress": job_progress(session, job_id),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "task_type": t.task_type,
                        "module_id": t.module_id,
                        "state": t.state.value,
                        "quality_state": t.quality_state.value,
                        "attempt": t.attempt,
                        "max_attempts": t.max_attempts,
                        "idempotency_key": t.idempotency_key,
                        "error_code": t.error_code,
                        "output_manifest": t.output_manifest,
                    }
                    for t in tasks
                ],
            }
        _emit(payload)
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command()
def rerun(
    paper_id: str = typer.Argument(help="Paper ID (pap_...)."),
    stage: str = typer.Option(
        "EVIDENCE_INDEXED",
        "--stage",
        help="Target pipeline stage to (re)run, e.g. STRUCTURED, EVIDENCE_INDEXED.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Configured model role for verification: analyst, verifier, synthesizer.",
    ),
) -> None:
    """Run/replay ONE stage for a paper's latest version (P05).

    Creates a job for the version if none exists, then replays exactly the
    requested stage (fresh idempotency key; other stages untouched).
    """
    from sqlalchemy import select  # noqa: PLC0415

    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.database.models import JobRow, PaperRow, PaperVersionRow  # noqa: PLC0415
    from paperintel.schemas.enums import PipelineStage  # noqa: PLC0415
    from paperintel.workflow import engine as workflow_engine  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    try:
        wanted_stage = PipelineStage(
            "VERIFIED" if stage.lower() == "verification" else stage.upper()
        )
    except ValueError:
        _fail_domain_error(
            DomainError(
                "CFG_002",
                message=f"Unknown pipeline stage: {stage!r}",
                details={"stage": stage},
            )
        )
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    from paperintel.database.base import session_scope  # noqa: PLC0415

    try:
        with session_scope(factory) as session:
            paper = session.get(PaperRow, paper_id)
            if paper is None:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message=f"Unknown paper ID: {paper_id}",
                        details={"paper_id": paper_id},
                    )
                )
                return
            version = session.scalars(
                select(PaperVersionRow)
                .where(PaperVersionRow.paper_id == paper_id)
                .order_by(PaperVersionRow.created_at.desc())
            ).first()
            if version is None:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message="Paper has no imported versions.",
                        details={"paper_id": paper_id},
                    )
                )
                return
            report_path = (
                Path(settings.core.data_dir)
                / "cache"
                / "extraction_reports"
                / f"{version.content_sha256}.json"
            )
            existing_job = session.scalars(
                select(JobRow)
                .where(JobRow.paper_version_id == version.paper_version_id)
                .order_by(JobRow.created_at.desc())
            ).first()
            if existing_job is None:
                created = workflow_engine.create_job(
                    session,
                    paper_id=paper_id,
                    paper_version_id=version.paper_version_id,
                    current_stage=PipelineStage.EXTRACTED,
                )
                workflow_engine.plan_job(
                    session,
                    created,
                    data_dir=str(settings.core.data_dir),
                    content_sha256=version.content_sha256,
                    report_path=str(report_path),
                )
                job_id = created.job_id
                planned = True
            else:
                job_id = existing_job.job_id
                planned = False
            base_manifest = {
                "paper_version_id": version.paper_version_id,
                "content_sha256": version.content_sha256,
                "config_hash": workflow_engine.build_base_manifest(
                    paper_version_id=version.paper_version_id,
                    content_sha256=version.content_sha256,
                    report_path=str(report_path),
                    data_dir=str(settings.core.data_dir),
                )["config_hash"],
                "scope_hash": version.content_sha256[:16],
                "report_path": str(report_path),
                "data_dir": str(settings.core.data_dir),
            }
            if model is not None:
                from paperintel.config.fingerprint import analysis_config_hash
                from paperintel.schemas.enums import ModelRole

                try:
                    role = ModelRole(model)
                except ValueError as exc:
                    raise DomainError("CFG_002", message="Unknown model role.") from exc
                if wanted_stage is not PipelineStage.VERIFIED:
                    raise DomainError("CFG_002", message="--model requires --stage verification.")
                base_manifest["model_role"] = role.value
                base_manifest["config_hash"] = analysis_config_hash(
                    stage="verification", model_role=role.value
                )
            task = workflow_engine.replay_stage(
                session, job_id, wanted_stage, input_manifest=base_manifest
            )
            payload = {
                "job_id": job_id,
                "paper_id": paper_id,
                "paper_version_id": version.paper_version_id,
                "stage": wanted_stage.value,
                "task_id": task.task_id,
                "task_state": task.state.value,
                "job_planned_now": planned,
                "note": "stage replay task queued; use paperctl jobs/job to watch",
            }
        _emit(payload)
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command()
def replay(
    task_id: str = typer.Argument(help="Task ID (tsk_...) to re-execute."),
    eager: bool = typer.Option(
        False, "--eager", help="Execute synchronously in-process (no broker)."
    ),
) -> None:
    """Replay one task: re-claim and re-execute it through the standard
    runner (P05). Idempotency keys guard canonical outputs."""
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.database.models import TaskRow  # noqa: PLC0415
    from paperintel.workflow.celery_app import run_task_once, submit_task  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message=f"Unknown task ID: {task_id}",
                        details={"task_id": task_id},
                    )
                )
                return
            if row.state.value in (
                "SUCCEEDED",
                "SUCCEEDED_WITH_WARNINGS",
                "FAILED",
                "CANCELLED",
                "SKIPPED",
            ):
                _fail_domain_error(
                    DomainError(
                        "INTERNAL_002",
                        message=(
                            f"Task {task_id} is terminal ({row.state.value}); "
                            "use 'paperctl rerun' to replay a stage instead."
                        ),
                        details={"task_id": task_id, "state": row.state.value},
                    )
                )
                return
            database_url = settings.database.url
        if eager:
            with factory() as session:
                outcome = run_task_once(session, task_id)
                session.commit()
            _emit({"replayed": outcome})
        else:
            result = submit_task(task_id, database_url=database_url)
            _emit({"replayed": {"submitted": result}})
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command()
def trace(
    trace_id: str = typer.Argument(help="Trace ID (trc_...)."),
) -> None:
    """Chronological trace events for one trace (P05, doc 04 §12)."""
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
    )
    from paperintel.operations.debug import redact
    from paperintel.operations.tracing import read_trace  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            events = read_trace(session, trace_id)
            if not events:
                _fail_domain_error(
                    DomainError(
                        "CFG_002",
                        message=f"No trace events found for {trace_id!r}",
                        details={"trace_id": trace_id},
                    )
                )
                return
            payload = {
                "trace_id": trace_id,
                "events": [
                    {
                        "occurred_at": e.occurred_at.isoformat(),
                        "kind": e.kind,
                        "level": e.level,
                        "module_id": e.module_id,
                        "job_id": e.job_id,
                        "task_id": e.task_id,
                        "message": redact(e.message),
                        "data": redact(e.data),
                    }
                    for e in events
                ],
            }
        _emit(payload)
    except DomainError as exc:
        _fail_domain_error(exc)
    finally:
        engine.dispose()


@app.command("debug-bundle")
def debug_bundle(
    job_or_trace_id: str = typer.Argument(help="Job ID (job_...) or trace ID (trc_...)."),
    output: str | None = typer.Option(None, "--output", help="Write diagnostic ZIP to this path."),
) -> None:
    """Redacted debug bundle for a job or trace (P12).

    Contains job/task state, trace events, model-call audit rows and
    failure details with SECRETS REDACTED (doc 04 §13)."""
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )
    from paperintel.operations.debug import write_debug_zip  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            destination = (
                Path(output)
                if output
                else Path(settings.core.data_dir) / "objects" / "debug" / f"{job_or_trace_id}.zip"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_debug_zip(session, job_or_trace_id, destination, settings=settings)
    except DomainError as exc:
        _fail_domain_error(exc)
        return
    finally:
        engine.dispose()
    _emit({"written": str(destination), "bytes": destination.stat().st_size, "format": "zip"})


@app.command()
def disk() -> None:
    """Disk usage by retention class + free-space health (P10)."""
    from paperintel.operations.disk import disk_report  # noqa: PLC0415

    settings = _settings_or_fail()
    if settings is None:
        return
    _emit(disk_report(settings.core.data_dir, settings=settings))


@app.command()
def gc(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report candidates without deleting anything."
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Actually delete the reported candidates."
    ),
) -> None:
    """Retention-aware garbage collection (P10).

    Use --dry-run to inspect candidates; bare gc executes policy-safe collection.
    """
    from paperintel.database.base import (  # noqa: PLC0415
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )
    from paperintel.operations.gc import run_gc  # noqa: PLC0415

    if dry_run and execute:
        _fail_domain_error(
            DomainError(
                "CFG_002",
                message="--dry-run and --execute are mutually exclusive.",
                details={},
            )
        )
        return

    settings = _settings_or_fail()
    if settings is None:
        return
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            report = run_gc(
                session,
                settings.core.data_dir,
                dry_run=dry_run,
                settings=settings,
            )
    except DomainError as exc:
        _fail_domain_error(exc)
        return

    _emit(
        {
            "dry_run": report.dry_run,
            "candidates": [
                {
                    "object": candidate.object_path,
                    "size_bytes": candidate.size_bytes,
                    "retention_class": candidate.retention_class,
                    "reason": candidate.reason,
                    "last_reference": candidate.last_reference,
                }
                for candidate in report.candidates
            ],
            "protected_canonical": [
                {
                    "object": entry.object_path,
                    "size_bytes": entry.size_bytes,
                    "retention_class": entry.retention_class,
                    "reason": entry.reason,
                    "last_reference": entry.last_reference,
                }
                for entry in report.protected
            ],
            "reclaimable_bytes": report.candidate_bytes,
            "reclaimed_bytes": report.reclaimed_bytes,
            "removed": report.removed,
            "errors": report.errors,
        }
    )


def _settings_or_fail():
    try:
        return load_config(None)
    except DomainError as exc:
        _fail_domain_error(exc)
        return None


def _count_nodes(node: Any) -> int:
    return 1 + sum(_count_nodes(child) for child in node.children)


def _classes_by_count(tree: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}

    def visit(node: Any) -> None:
        key = node.normalized_class.value
        counts[key] = counts.get(key, 0) + 1
        for child in node.children:
            visit(child)

    for root in tree:
        visit(root)
    return dict(sorted(counts.items()))


def app_entry() -> None:
    """Console-script entry point (paperctl)."""
    try:
        app()
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as an envelope, never swallowed
        envelope = {
            "error": {
                "code": "INTERNAL_001",
                "message": "Unexpected internal error.",
                "retryable": False,
                "severity": "CRITICAL",
                "trace_id": None,
                "details": {"type": type(exc).__name__},
            }
        }
        typer.echo(json.dumps(envelope, indent=2), err=True)
        # Full trace goes to stderr for operators, not through API responses.
        print(f"internal detail: {exc!r}", file=sys.stderr)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    app_entry()
