# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Centralized health monitoring module for MEHO.

Provides three-tier health checks:
- /health: Liveness probe (zero I/O, always returns healthy if process is alive)
- /ready: Readiness probe (checks PostgreSQL, Redis, Keycloak in parallel)
- /status: Full diagnostic (adds LLM availability with 60s cache, uptime, version)

Also provides startup config validation with operator-friendly error messages.
"""

import asyncio
import sys
import time
from typing import Any

import httpx

# Constants
CHECK_TIMEOUT = 5.0  # per-check timeout in seconds
LLM_CHECK_TIMEOUT = 10.0  # LLM probe can be slower
LLM_CACHE_TTL = 60.0  # seconds between LLM probes
APP_VERSION = "1.67.0"

# Module-level state
_llm_cache: dict[str, Any] = {}
_llm_cache_lock = asyncio.Lock()
_app_start_time: float = time.monotonic()


def _elapsed(start: float) -> int:
    """Return milliseconds elapsed since start."""
    return int((time.monotonic() - start) * 1000)


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


async def check_postgres() -> dict:
    """Check PostgreSQL connectivity via SELECT 1."""
    from sqlalchemy import text

    from meho_app.database import get_engine

    start = time.monotonic()
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")),
                timeout=CHECK_TIMEOUT,
            )
        return {"name": "postgres", "status": "pass", "latency_ms": _elapsed(start)}
    except Exception as e:
        return {
            "name": "postgres",
            "status": "fail",
            "error": str(e),
            "latency_ms": _elapsed(start),
        }


async def check_redis() -> dict:
    """Check Redis connectivity via PING."""
    from meho_app.core.config import get_config
    from meho_app.core.redis import get_redis_client

    start = time.monotonic()
    try:
        config = get_config()
        client = await get_redis_client(config.redis_url)
        await asyncio.wait_for(client.ping(), timeout=CHECK_TIMEOUT)
        return {"name": "redis", "status": "pass", "latency_ms": _elapsed(start)}
    except Exception as e:
        return {"name": "redis", "status": "fail", "error": str(e), "latency_ms": _elapsed(start)}


async def check_keycloak() -> dict:
    """Check Keycloak reachability via its own health endpoint."""
    from meho_app.api.config import get_api_config

    start = time.monotonic()
    try:
        api_config = get_api_config()
        url = f"{api_config.keycloak_url}/health/ready"
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            response = await client.get(url)
            response.raise_for_status()
        return {"name": "keycloak", "status": "pass", "latency_ms": _elapsed(start)}
    except Exception as e:
        return {
            "name": "keycloak",
            "status": "fail",
            "error": str(e),
            "latency_ms": _elapsed(start),
        }


async def check_llm() -> dict:
    """
    Check LLM availability with 60-second shared cache.

    Uses asyncio.Lock to prevent thundering herd: only one probe runs at a time,
    all other callers get the cached result.
    """
    async with _llm_cache_lock:
        now = time.monotonic()
        if "result" in _llm_cache and (now - _llm_cache["timestamp"]) < LLM_CACHE_TTL:
            cached = _llm_cache["result"].copy()
            cached["cached"] = True
            return cached

    start = time.monotonic()
    try:
        from pydantic_ai import Agent

        from meho_app.core.config import get_config

        config = get_config()
        agent = Agent(config.llm_model)
        await asyncio.wait_for(
            agent.run("ping"),
            timeout=LLM_CHECK_TIMEOUT,
        )
        probe_result = {"name": "llm", "status": "pass", "latency_ms": _elapsed(start)}
    except Exception as e:
        probe_result = {
            "name": "llm",
            "status": "fail",
            "error": str(e),
            "latency_ms": _elapsed(start),
        }

    async with _llm_cache_lock:
        _llm_cache["result"] = probe_result
        _llm_cache["timestamp"] = time.monotonic()

    return probe_result


# ---------------------------------------------------------------------------
# Aggregator functions
# ---------------------------------------------------------------------------


async def check_ready() -> tuple[bool, list[dict]]:
    """
    Run all readiness checks in parallel.

    Returns (all_pass, results) where results is a list of check dicts.
    Uses asyncio.gather for parallel execution.
    """
    results = await asyncio.gather(
        check_postgres(),
        check_redis(),
        check_keycloak(),
        return_exceptions=True,
    )

    checks: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            checks.append({"name": "unknown", "status": "fail", "error": str(r)})
        else:
            checks.append(r)

    all_pass = all(c["status"] == "pass" for c in checks)
    return all_pass, checks


async def check_status() -> dict:
    """
    Full diagnostic check including LLM availability.

    Returns status, version, uptime, and all check results.
    Status is "healthy" if all pass, "degraded" if non-critical (LLM) fails,
    "unhealthy" if critical (DB/Redis/Keycloak) fails.
    """
    ready_result, llm_result = await asyncio.gather(
        check_ready(),
        check_llm(),
        return_exceptions=True,
    )

    # Unpack readiness results
    if isinstance(ready_result, Exception):
        all_critical_pass = False
        ready_checks: list[dict] = [
            {"name": "readiness", "status": "fail", "error": str(ready_result)}
        ]
    else:
        all_critical_pass, ready_checks = ready_result

    # Unpack LLM result
    if isinstance(llm_result, Exception):
        llm_check = {"name": "llm", "status": "fail", "error": str(llm_result)}
        llm_pass = False
    else:
        llm_check = llm_result
        llm_pass = llm_check["status"] == "pass"

    # Determine overall status
    if all_critical_pass and llm_pass:
        overall_status = "healthy"
    elif all_critical_pass:
        overall_status = "degraded"
    else:
        overall_status = "unhealthy"

    # Build checks dict keyed by name
    checks_dict = {c["name"]: c for c in ready_checks}
    checks_dict["llm"] = llm_check

    uptime = int(time.monotonic() - _app_start_time)

    return {
        "status": overall_status,
        "version": APP_VERSION,
        "uptime_seconds": uptime,
        "checks": checks_dict,
    }


# ---------------------------------------------------------------------------
# Startup config validation
# ---------------------------------------------------------------------------

def _get_llm_config_vars() -> list[str]:
    """Get provider-specific config vars for LLM health check.

    Phase 82: Multi-LLM Support -- checks the correct API key/URL
    based on the active LLM provider.
    """
    try:
        from meho_app.core.config import get_config

        provider = get_config().llm_provider
        return {
            "anthropic": ["anthropic_api_key"],
            "openai": ["openai_api_key"],
            "ollama": ["ollama_base_url"],
        }.get(provider, ["anthropic_api_key"])
    except Exception:
        return ["anthropic_api_key"]  # Fallback to current behavior


# Config var groupings for operator-friendly output
_CONFIG_GROUPS = {
    "Database": {
        "vars": ["database_url"],
        "critical": True,
    },
    "Cache": {
        "vars": ["redis_url"],
        "critical": True,
    },
    "LLM": {
        "vars": _get_llm_config_vars(),
        "critical": True,
    },
    "Security": {
        "vars": ["credential_encryption_key"],
        "critical": True,
    },
    "Embeddings": {
        "vars": ["voyage_api_key"],
        "critical": False,
    },
    "Storage": {
        "vars": [
            "object_storage_endpoint",
            "object_storage_access_key",
            "object_storage_secret_key",
        ],
        "critical": False,
    },
}


def validate_startup_config():
    """
    Validate config at startup with operator-friendly error messages.

    Prints a structured checklist to stderr grouping config vars by service.
    Exits with code 1 if any critical variable is missing.
    Returns the Config instance on success.
    """
    from pydantic import ValidationError

    from meho_app.core.config import Config

    try:
        config = Config()
        # Success -- print confirmation
        print("=== MEHO Startup Config ===", file=sys.stderr)
        for group_name, group_info in _CONFIG_GROUPS.items():
            critical_label = " (CRITICAL)" if group_info["critical"] else " (optional)"
            print(f"\n  {group_name}{critical_label}:", file=sys.stderr)
            for var in group_info["vars"]:
                print(f"    [OK] {var.upper()}", file=sys.stderr)
        print("\n  All config validated.\n", file=sys.stderr)
        return config
    except (ValidationError, Exception) as e:
        # Parse missing fields
        missing_fields: set[str] = set()
        if hasattr(e, "errors"):
            for err in e.errors():
                if err.get("type") == "missing":
                    loc = err.get("loc", ())
                    if loc:
                        missing_fields.add(str(loc[0]))

        has_critical_missing = False

        print("\n=== MEHO Startup Config ===", file=sys.stderr)
        for group_name, group_info in _CONFIG_GROUPS.items():
            critical_label = " (CRITICAL)" if group_info["critical"] else " (optional)"
            print(f"\n  {group_name}{critical_label}:", file=sys.stderr)
            for var in group_info["vars"]:
                if var in missing_fields:
                    status = "[MISSING]"
                    if group_info["critical"]:
                        has_critical_missing = True
                else:
                    status = "[OK]"
                print(f"    {status} {var.upper()}", file=sys.stderr)

        if has_critical_missing:
            print(
                "\n  FATAL: Critical configuration missing. "
                "Set the MISSING environment variables and restart.\n",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            # Only optional vars missing -- warn but continue
            print(
                "\n  WARNING: Optional configuration missing. Some features may be unavailable.\n",
                file=sys.stderr,
            )
            # Re-raise original error if it wasn't just missing optional fields
            # This shouldn't happen in practice since Pydantic only errors on required fields
            raise
