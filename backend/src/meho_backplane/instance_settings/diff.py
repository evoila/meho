# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The settings diff: added, changed (before and after), and "not in file".

Both sides are a *spec view*: ``{tenant_id: tenant_block}`` where each block is
in the export's shape (:func:`meho_backplane.instance_settings.service.export_tenant_block`):

.. code-block:: python

    {"id": "...", "policies": {...}, "conventions": [{...}], "targets": [{...}]}

The preview diffs the live state against the state the file would produce;
``GET /api/v1/settings/history`` diffs a revision's ``spec_before`` against its
``spec_after``. Nothing here touches the database.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["DEFAULT_POLICIES", "SECTIONS", "diff_specs", "is_empty", "summarise"]

#: What a tenant MEHO has never seen gets (the ``tenant`` column defaults).
DEFAULT_POLICIES: Final[dict[str, Any]] = {
    "announce_gate_enabled": False,
    "flight_recorder": {"enabled": False, "retention_days": None, "agent_readable": None},
    "mail_recipient_allowlist": None,
}

SECTIONS: Final[tuple[str, ...]] = ("tenants", "policies", "conventions", "targets")

_ITEM_KEYS: Final[dict[str, str]] = {"conventions": "slug", "targets": "name"}

SpecView = dict[str, dict[str, Any]]


def _empty_section() -> dict[str, list[dict[str, Any]]]:
    return {"added": [], "changed": [], "not_in_file": []}


def _flatten_policies(policies: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in policies.items():
        if isinstance(value, dict):
            for sub, subvalue in value.items():
                flat[f"{key}.{sub}"] = subvalue
        else:
            flat[key] = value
    return flat


_MISSING: Final = object()


def _field_changes(
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    keys = [k for k in after if before.get(k, _MISSING) != after[k]]
    keys += [k for k in before if k not in after]
    return (
        {k: before.get(k) for k in keys},
        {k: after.get(k) for k in keys},
    )


def diff_specs(
    before: SpecView,
    after: SpecView,
    *,
    in_file: dict[str, dict[str, set[str]]] | None = None,
    tenants_not_in_file: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Diff two spec views section by section.

    A tenant in *after* but not in *before* is new: it is reported under
    ``tenants.added`` and its policies are diffed against the defaults a new
    tenant gets. *in_file* maps tenant id -> section -> item keys the file
    names; an item in *before* whose key is missing there is reported under
    ``not_in_file``. ``None`` (the history view) reports no "not in file".
    """
    result = {section: _empty_section() for section in SECTIONS}
    for tenant_id in sorted(set(after) - set(before)):
        result["tenants"]["added"].append({"tenant": tenant_id})
    for tenant_id in tenants_not_in_file or ():
        result["tenants"]["not_in_file"].append({"tenant": tenant_id})

    for tenant_id in sorted(after):
        old = before.get(tenant_id) or {
            "policies": DEFAULT_POLICIES,
            "conventions": [],
            "targets": [],
        }
        new = after[tenant_id]

        old_policies = _flatten_policies(old.get("policies") or {})
        for key, value in _flatten_policies(new.get("policies") or {}).items():
            if key in old_policies and old_policies[key] == value:
                continue
            result["policies"]["changed"].append(
                {"tenant": tenant_id, "key": key, "before": old_policies.get(key), "after": value}
            )

        for section, item_key in _ITEM_KEYS.items():
            old_items = {item[item_key]: item for item in old.get(section) or []}
            new_items = {item[item_key]: item for item in new.get(section) or []}
            for key in sorted(new_items):
                if key not in old_items:
                    result[section]["added"].append(
                        {"tenant": tenant_id, "key": key, "value": new_items[key]}
                    )
                    continue
                if old_items[key] == new_items[key]:
                    continue
                before_fields, after_fields = _field_changes(old_items[key], new_items[key])
                result[section]["changed"].append(
                    {
                        "tenant": tenant_id,
                        "key": key,
                        "before": before_fields,
                        "after": after_fields,
                    }
                )
            if in_file is None:
                continue
            named = in_file.get(tenant_id, {}).get(section, set())
            for key in sorted(old_items):
                if key not in named:
                    result[section]["not_in_file"].append({"tenant": tenant_id, "key": key})
    return result


def is_empty(diff: dict[str, dict[str, list[Any]]]) -> bool:
    """True when nothing would be added or changed ("not in file" does not count)."""
    return all(not diff[s]["added"] and not diff[s]["changed"] for s in SECTIONS)


def summarise(diff: dict[str, dict[str, list[Any]]]) -> dict[str, dict[str, int]]:
    """Added and changed counts per section -- the revision's ``summary``."""
    return {
        section: {"added": len(diff[section]["added"]), "changed": len(diff[section]["changed"])}
        for section in SECTIONS
    }
