# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Verify ``env.example`` stays in sync with Pydantic Settings.

Cross-references every ``BaseSettings`` subclass under :mod:`meho_app` against
``env.example`` and reports drift. The script is the canonical answer to the
question that motivated removing bash ``.env`` sourcing from ``dev-env.sh``:
"if Pydantic is the single loader of ``.env``, what guarantees ``.env``-style
files document every variable Pydantic actually reads?".

Four failure modes are reported:

* **MISSING_FROM_EXAMPLE** -- a Settings field has no entry (commented or
  uncommented) in ``env.example`` and is not in
  ``SETTINGS_INTERNAL_ALLOW_LIST``. New configuration variables MUST be
  either documented in ``env.example`` or explicitly marked as internal /
  advanced.
* **ORPHAN_IN_EXAMPLE** -- an entry in ``env.example`` does not map to any
  Settings field and is not in the explicit ``EXTERNAL_ALLOW_LIST`` of
  variables consumed outside Pydantic (postgres image, Vite build, OTEL SDK,
  etc.). Either delete the entry or add it to the allow-list with a comment
  explaining the consumer.
* **DEAD_EXTERNAL_VAR** -- a variable is in ``EXTERNAL_ALLOW_LIST`` but no
  longer appears in ``env.example``.
* **DEAD_INTERNAL_VAR** -- a variable is in ``SETTINGS_INTERNAL_ALLOW_LIST``
  but no Settings field reads it any more.

Exit codes:

* ``0`` -- in sync.
* ``1`` -- drift detected (CI / pre-commit fails).

Run via ``./scripts/check-env-example-sync.py`` or ``uv run python
scripts/check-env-example-sync.py``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_settings import BaseSettings

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / "env.example"


EXTERNAL_ALLOW_LIST: dict[str, str] = {
    "POSTGRES_USER": "consumed by the postgres image (init), not Pydantic",
    "POSTGRES_PASSWORD": "consumed by the postgres image (init), not Pydantic",
    "POSTGRES_DB": "consumed by the postgres image (init), not Pydantic",
    "OPENAI_API_KEY": "read directly by the openai SDK when LLM_PROVIDER=openai",
    "OLLAMA_BASE_URL": "read directly by the ollama client when LLM_PROVIDER=ollama",
    "KEYCLOAK_URL": "read by the frontend Keycloak client and setup-keycloak.sh",
    "KEYCLOAK_REALM": "read by the frontend Keycloak client and setup-keycloak.sh",
    "KEYCLOAK_ADMIN_PASSWORD": "consumed by the keycloak image (admin bootstrap)",
    "VITE_KEYCLOAK_URL": "frontend Vite build-time variable",
    "VITE_KEYCLOAK_REALM": "frontend Vite build-time variable",
    "VITE_KEYCLOAK_CLIENT_ID": "frontend Vite build-time variable",
    "FRONTEND_API_URL": "frontend runtime override",
    "OTEL_SERVICE_NAME": "OpenTelemetry SDK reads this directly",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "OpenTelemetry SDK reads this directly",
    "OTEL_CONSOLE": "MEHO logging hook reads this directly (logging_setup.py)",
    "OTEL_TRACE_LEVEL": "MEHO observability layer reads this directly",
    "OTEL_MAX_BODY_SIZE": "MEHO observability layer reads this directly",
    "MEHO_LOG_LEVEL": "MEHO logging hook reads this directly (logging_setup.py)",
    "APP_ENVIRONMENT": (
        "legacy alias documented for clarity; the actual Pydantic-driven "
        "field is ENV (see Config.env). Keep until /docs/getting-started "
        "explicitly documents ENV instead."
    ),
}


SETTINGS_INTERNAL_ALLOW_LIST: dict[str, str] = {
    # Service plumbing -- set explicitly by docker-compose, rarely overridden
    "AGENT_SERVICE_PORT": "set by docker-compose; not user-tunable",
    "AGENT_SERVICE_URL": "BFF -> agent service URL; set by docker-compose",
    "INGESTION_SERVICE_PORT": "set by docker-compose; not user-tunable",
    "INGESTION_SERVICE_URL": "BFF -> ingestion service URL; set by docker-compose",
    "KNOWLEDGE_SERVICE_PORT": "set by docker-compose; not user-tunable",
    "KNOWLEDGE_SERVICE_URL": "BFF -> knowledge service URL; set by docker-compose",
    "OPENAPI_SERVICE_PORT": "set by docker-compose; not user-tunable",
    "OPENAPI_SERVICE_URL": "BFF -> openapi service URL; set by docker-compose",
    "API_HOST": "uvicorn binds to 0.0.0.0; only changed by deployment configs",
    "API_PORT": "set by deployment configs; container exposes 8000",
    # Environment naming -- ENV is the active Pydantic field but APP_ENVIRONMENT
    # is the alias that env.example documents (see EXTERNAL_ALLOW_LIST entry).
    "ENV": "documented as APP_ENVIRONMENT alias in env.example (cleanup pending)",
    "ENVIRONMENT": "MEHOAPIConfig field; documented as APP_ENVIRONMENT alias",
    "LOG_LEVEL": "MEHO_LOG_LEVEL takes precedence; LOG_LEVEL is the Pydantic field",
    # Keycloak admin -- only the password is operator-tunable
    "JWKS_CACHE_TTL": "advanced -- OIDC keys cache TTL; default fits >99% of deployments",
    "KEYCLOAK_ADMIN_USERNAME": "always 'admin' for the bundled Keycloak; only password is tunable",
    # Open-core / enterprise
    "LICENSE_KEY": "Phase 80 enterprise license; documented in deployment.md",
    # Ingestion tuning -- defaults are correct, advanced overrides only
    "INGESTION_DEVICE": "Docling accelerator selector; auto-probes",
    "INGESTION_MAX_WORKERS": "Docling worker pool; tune only when CPU-constrained",
    "INGESTION_NUM_THREADS": "Docling thread count; mirrors official Docling default",
    # Ephemeral ingestion backend -- gated by MEHO_FEATURE_EPHEMERAL_INGESTION
    "MEHO_INGESTION_BACKEND": "honored only when MEHO_FEATURE_EPHEMERAL_INGESTION=true",
    "MEHO_INGESTION_OFFLOAD_THRESHOLD": ("honored only when MEHO_FEATURE_EPHEMERAL_INGESTION=true"),
    "MEHO_K8S_CA_CERT": "honored only when MEHO_INGESTION_BACKEND=kubernetes",
    "MEHO_K8S_NAMESPACE": "honored only when MEHO_INGESTION_BACKEND=kubernetes",
    "MEHO_K8S_SERVER": "honored only when MEHO_INGESTION_BACKEND=kubernetes",
    "MEHO_K8S_SERVICE_ACCOUNT": "honored only when MEHO_INGESTION_BACKEND=kubernetes",
    "MEHO_K8S_TOKEN": "honored only when MEHO_INGESTION_BACKEND=kubernetes",
    "MEHO_CLOUDRUN_JOB_NAME": "honored only when MEHO_INGESTION_BACKEND=cloudrun",
    "MEHO_CLOUDRUN_PROJECT": "honored only when MEHO_INGESTION_BACKEND=cloudrun",
    "MEHO_CLOUDRUN_REGION": "honored only when MEHO_INGESTION_BACKEND=cloudrun",
    "MEHO_DOCKER_HOST": "honored only when MEHO_INGESTION_BACKEND=docker",
    "MEHO_WORKER_IMAGE": "honored only for K8s/CloudRun/Docker ingestion backends",
    # MCP integration is alpha and not yet surfaced to operators
    "MEHO_FEATURE_MCP_CLIENT": "MCP integration is alpha; not surfaced to operators yet",
    "MEHO_FEATURE_MCP_SERVER": "MCP integration is alpha; not surfaced to operators yet",
    # Advanced LLM / observability tuning -- defaults work, override in production
    "RATE_LIMIT_CLEANUP": "Pydantic default is correct for community; tune in production",
    "RATE_LIMIT_EXPORT": "Pydantic default is correct for community; tune in production",
    "RATE_LIMIT_SEARCH": "Pydantic default is correct for community; tune in production",
    "RATE_LIMIT_TRANSCRIPT": "Pydantic default is correct for community; tune in production",
    "TOPOLOGY_AUTO_DISCOVERY_ENABLED": "default true is correct; surfaced via MEHO_FEATURE_TOPOLOGY",
    "TOPOLOGY_DISCOVERY_BATCH_SIZE": "advanced topology tuning",
    "TOPOLOGY_DISCOVERY_INTERVAL_SECONDS": "advanced topology tuning",
    "TOPOLOGY_DISCOVERY_QUEUE_KEY": "Redis key naming -- internal",
    "TRANSCRIPT_GRACE_DAYS": "advanced retention tuning",
    "TRANSCRIPT_RETENTION_DAYS": "advanced retention tuning",
    "SUGGESTION_AUTO_APPROVE_THRESHOLD": "advanced topology suggestion tuning",
    "SUGGESTION_LLM_APPROVE_CONFIDENCE": "advanced topology suggestion tuning",
    "SUGGESTION_LLM_VERIFY_THRESHOLD": "advanced topology suggestion tuning",
    # Endpoint search -- TASK-126 trial; safe default, surfaced in docs
    "ENDPOINT_SEARCH_ALGORITHM": "TASK-126 trial; safe default, surfaced in docs",
}


SETTINGS_MODULES: tuple[str, ...] = (
    "meho_app.core.config",
    "meho_app.core.feature_flags",
    "meho_app.api.config",
)


def _expected_env_names(field_name: str, field: FieldInfo, env_prefix: str) -> set[str]:
    """
    Compute the env var name(s) Pydantic Settings would read for this field.

    Settings respects (in order): ``validation_alias`` > ``alias`` > the field
    name itself (uppercased), all of them prefixed by ``env_prefix`` from the
    model's ``SettingsConfigDict`` when set.
    """
    names: set[str] = set()

    alias = field.alias or field.validation_alias
    if alias is None:
        names.add(field_name.upper())
    elif isinstance(alias, str):
        names.add(alias.upper())
    else:
        from pydantic import AliasChoices

        if isinstance(alias, AliasChoices):
            for choice in alias.choices:
                if isinstance(choice, str):
                    names.add(choice.upper())
                elif isinstance(choice, list):
                    for inner in choice:
                        if isinstance(inner, str):
                            names.add(inner.upper())

    return {f"{env_prefix}{name}" if env_prefix else name for name in names}


def _load_settings_classes() -> list[type[BaseSettings]]:
    import importlib

    classes: list[type[BaseSettings]] = []
    for dotted in SETTINGS_MODULES:
        module = importlib.import_module(dotted)
        classes.extend(
            obj
            for obj in vars(module).values()
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseSettings)
                and obj is not BaseSettings
                and obj.__module__ == dotted
            )
        )
    return classes


def _collect_settings_env_vars() -> dict[str, str]:
    """
    Map every env var name the application's Settings classes read to the
    fully-qualified Settings field that owns it.
    """
    discovered: dict[str, str] = {}
    for cls in _load_settings_classes():
        env_prefix = (cls.model_config.get("env_prefix") or "") if cls.model_config else ""
        for field_name, field in cls.model_fields.items():
            if field_name.startswith("_"):
                continue
            for env_name in _expected_env_names(field_name, field, env_prefix):
                discovered[env_name] = f"{cls.__module__}.{cls.__name__}.{field_name}"
    return discovered


_ENV_LINE_RE = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=")


def _collect_env_example_vars(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_LINE_RE.match(line)
        if match:
            names.add(match.group(1))
    return names


def main() -> int:
    if not ENV_EXAMPLE.exists():
        print(f"error: {ENV_EXAMPLE.relative_to(REPO_ROOT)} is missing", file=sys.stderr)
        return 1

    settings_vars = _collect_settings_env_vars()
    example_vars = _collect_env_example_vars(ENV_EXAMPLE)

    missing_from_example = sorted(
        set(settings_vars) - example_vars - set(SETTINGS_INTERNAL_ALLOW_LIST)
    )
    orphans = sorted(example_vars - set(settings_vars) - set(EXTERNAL_ALLOW_LIST))
    dead_external = sorted(set(EXTERNAL_ALLOW_LIST) - example_vars)
    dead_internal = sorted(set(SETTINGS_INTERNAL_ALLOW_LIST) - set(settings_vars))

    failed = False

    if missing_from_example:
        failed = True
        print(
            "MISSING_FROM_EXAMPLE -- Settings fields with no env.example entry "
            "and no SETTINGS_INTERNAL_ALLOW_LIST justification:"
        )
        for name in missing_from_example:
            print(f"  {name}  (defined in {settings_vars[name]})")
        print(
            "Either document the variable in env.example or add it to "
            "SETTINGS_INTERNAL_ALLOW_LIST in scripts/check-env-example-sync.py "
            "with a one-line note explaining why it stays internal."
        )
        print()

    if orphans:
        failed = True
        print("ORPHAN_IN_EXAMPLE -- env.example entries with no Pydantic consumer:")
        for name in orphans:
            print(f"  {name}")
        print(
            "Either remove these from env.example or add them to "
            "EXTERNAL_ALLOW_LIST in scripts/check-env-example-sync.py "
            "with a one-line note explaining the actual consumer."
        )
        print()

    if dead_external:
        failed = True
        print(
            "DEAD_EXTERNAL_VAR -- entries in EXTERNAL_ALLOW_LIST that are no longer in env.example:"
        )
        for name in dead_external:
            print(f"  {name}  ({EXTERNAL_ALLOW_LIST[name]})")
        print("Drop the matching entry from EXTERNAL_ALLOW_LIST or restore it to env.example.")
        print()

    if dead_internal:
        failed = True
        print(
            "DEAD_INTERNAL_VAR -- entries in SETTINGS_INTERNAL_ALLOW_LIST that no Settings "
            "field reads any more:"
        )
        for name in dead_internal:
            print(f"  {name}  ({SETTINGS_INTERNAL_ALLOW_LIST[name]})")
        print("Drop the matching entry from SETTINGS_INTERNAL_ALLOW_LIST.")
        print()

    if failed:
        return 1

    print(
        f"env.example is in sync with Pydantic Settings "
        f"({len(settings_vars)} Pydantic-managed vars, "
        f"{len(SETTINGS_INTERNAL_ALLOW_LIST)} marked internal, "
        f"{len(EXTERNAL_ALLOW_LIST)} external-only vars, "
        f"{len(example_vars)} entries in env.example)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
