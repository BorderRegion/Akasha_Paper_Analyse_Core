"""Additional deployment checks required by doc 04 §9."""

import os
from pathlib import Path

from paperintel.errors import DomainError


def required_fixtures_check() -> str:
    # Wheels/containers need not ship the source tree or test generators.
    candidates = [Path.cwd(), *Path(__file__).resolve().parents]
    for root in candidates:
        generator = root / "tests" / "fixtures" / "generators.py"
        try:
            if not generator.is_file():
                continue
            source = generator.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        return "PASS" if all(
            f"def build_{name}_" in source for name in ("f01", "f02", "f03", "f04", "f05", "f06")
        ) else "FAIL: incomplete fixture generators"
    return "UNKNOWN: test fixtures not installed"


def additional_checks(settings):
    from paperintel import schemas
    from paperintel.agents.prompts import BUILTIN_PROMPTS
    from paperintel.config.provider_config import load_provider_config
    from paperintel.providers.factory import build_registry
    from paperintel.workflow.celery_app import get_celery_app, tasks_eager

    checks = {}
    try:
        if settings.providers_file is None:
            checks["provider_configuration"] = "FAIL: no providers file configured"
            checks["environment_secrets"] = "FAIL: cannot determine required secrets"
        else:
            config = load_provider_config(settings.providers_file)
            missing = [
                entry.api_key_env
                for entry in config.providers.values()
                if entry.api_key_env and not os.environ.get(entry.api_key_env)
            ]
            checks["environment_secrets"] = (
                "FAIL: missing " + ", ".join(missing) if missing else "PASS"
            )
            build_registry(config)
            checks["provider_configuration"] = "PASS"
    except DomainError as exc:
        checks["provider_configuration"] = f"FAIL: {exc.code}"
    checks["prompt_manifests"] = (
        "PASS"
        if BUILTIN_PROMPTS
        and all(p.body and p.version and p.name for p in BUILTIN_PROMPTS.values())
        else "FAIL"
    )
    checks["schema_registration"] = (
        "PASS"
        if all(
            hasattr(schemas, name)
            for name in (
                "Claim",
                "Evidence",
                "AgentResult",
                "TaskContract",
                "JobContract",
                "GateReport",
            )
        )
        else "FAIL"
    )
    checks["required_fixtures"] = required_fixtures_check()
    try:
        workers = (
            {"eager": True} if tasks_eager() else get_celery_app().control.inspect(timeout=1).ping()
        )
        checks["worker_heartbeat"] = "PASS" if workers else "FAIL: no worker heartbeat"
    except Exception as exc:
        checks["worker_heartbeat"] = f"FAIL: {type(exc).__name__}"
    return checks
