"""Bounded, source-preserving training-protocol catalog.

This module has deliberately no dependency on a planner or workout writer.  A
catalog source is private intake, and a catalog protocol is a versioned source
prescription that may later be snapshotted into a plan.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_schemas import CatalogParameter, CatalogSpecification
from app.db.models import CatalogProtocol, CatalogSource

_DATA_PATH = Path(__file__).with_name("catalog_data.json")
_PUBLIC_STATUSES = {"ready"}
_SAM_VOLUME_ADAPTATIONS = {
    "1-on-1-off": ("reps", "sam_volume_reps"),
    "1-on-2-off": ("reps", "sam_volume_reps"),
    "continuity-5x3": ("sets", "sam_volume_sets"),
}


def _inventory() -> list[dict[str, Any]]:
    """Return versioned entries; fixture edits must bump inventory/entry version.

    Existing rows are immutable snapshots: changing fixture text at an existing
    version never rewrites them.  A fixture change therefore needs a positive
    ``inventory_version`` (or explicit per-entry ``version``) bump.
    """
    with _DATA_PATH.open(encoding="utf-8") as source:
        data = json.load(source)
    protocols = data.get("protocols")
    if not isinstance(protocols, list) or not protocols:
        raise RuntimeError("catalog inventory must be a non-empty protocol list")
    inventory_version = data.get("inventory_version")
    if (
        isinstance(inventory_version, bool)
        or not isinstance(inventory_version, int)
        or inventory_version < 1
    ):
        raise RuntimeError("catalog inventory_version must be a positive integer")
    versioned = []
    keys: set[tuple[str, int]] = set()
    for entry in protocols:
        if not isinstance(entry, dict):
            raise TypeError("catalog protocol entry must be an object")
        version = entry.get("version", inventory_version)
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RuntimeError("catalog protocol version must be a positive integer")
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            raise RuntimeError("catalog protocol needs a slug")
        key = (slug, version)
        if key in keys:
            raise RuntimeError("catalog inventory has duplicate slug/version")
        keys.add(key)
        versioned.append({**entry, "version": version})
    return versioned


def _specification(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Convert compact fixture data into the persistent, public-safe spec."""
    complete = entry.get(
        "source_completeness", "complete" if entry["status"] == "ready" else "partial"
    )
    spec = {
        "protocol_type": entry["category"],
        "original_category": entry["original_category"],
        "original_subcategory": entry.get("original_subcategory"),
        "source_completeness": complete,
        # A complete transcription can still be blocked on applicability/load
        # review. Preserve that reason instead of silently dropping it.
        "unresolved_fields": [entry["reason"]] if entry.get("reason") else [],
        "purpose": entry.get("purpose", []),
        "effects": entry.get("effects", []),
        "load_basis": entry.get("load_basis", {}),
        "duration": entry.get("duration", {}),
        "equipment": entry.get("equipment", {}),
        "location": entry.get("location", {}),
        "eligibility": entry.get("eligibility", {}),
        "restrictions": entry.get("restrictions", []),
        "steps": entry.get("steps", []),
        "adjustable_parameters": entry.get("adjustable_parameters", []),
        "default_parameters": entry.get("default_parameters", []),
    }
    adaptation = _SAM_VOLUME_ADAPTATIONS.get(entry["slug"])
    if adaptation:
        field, parameter_name = adaptation
        steps = spec["steps"]
        if (
            not steps
            or not isinstance(steps[0], Mapping)
            or not isinstance(steps[0].get(field), int)
        ):
            raise RuntimeError(f"Sam adaptation requires an integer root {field}: {entry['slug']}")
        source_count = steps[0][field]
        policy = {
            "policy": "sam_volume_reduction_v1",
            "provenance": "sam_adaptation_policy_not_source",
            "parameter_name": parameter_name,
            "root_step_field": field,
            "source_count": source_count,
            "minimum": 1,
            "transformation": "reduce_root_repeat_count_only",
        }
        spec["sam_adaptation_policy"] = policy
        spec["adjustable_parameters"] = [
            *spec["adjustable_parameters"],
            {"name": parameter_name, "unit": "count", "minimum": 1, "maximum": source_count},
        ]
        spec["default_parameters"] = [
            *spec["default_parameters"],
            {
                "name": parameter_name,
                "value": source_count,
                "unit": "count",
                "target": f"root_step.{field}",
            },
        ]
    # Validate the source form before it reaches a database row.  This also
    # guards against a future edit accidentally marking an incomplete card ready.
    CatalogSpecification.model_validate(spec)
    return spec


async def seed_catalog(session: AsyncSession) -> None:
    """Stage any missing built-in catalog rows without committing or making workouts."""
    inventory = _inventory()
    existing = set(
        (await session.execute(select(CatalogProtocol.slug, CatalogProtocol.version))).all()
    )
    for entry in inventory:
        key = (entry["slug"], entry["version"])
        if key in existing:
            continue
        session.add(
            CatalogProtocol(
                slug=entry["slug"],
                version=entry["version"],
                title=entry["title"],
                category=entry["category"],
                status=entry["status"],
                specification=_specification(entry),
            )
        )
    # Flush is intentionally not commit: a caller may use the new protocols in
    # the same transaction, while retaining complete transaction ownership.
    await session.flush()


def _as_strings(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.strip().casefold()} if value.strip() else set()
    if isinstance(value, Mapping):
        name = value.get("name") or value.get("id") or value.get("value")
        available = value.get("available")
        # A structured equipment entry deliberately needs an affirmative fact;
        # absence means unknown, not available.  Plain string lists remain a
        # legacy/convenience form whose presence is itself the affirmative fact.
        if available is not True:
            return set()
        return _as_strings(name)
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        result: set[str] = set()
        for item in value:
            result.update(_as_strings(item))
        return result
    return set()


def _profile_values(profile: Mapping[str, Any], key: str) -> set[str]:
    value = profile.get(key)
    if key != "equipment":
        # Location and simple profile values do not use the equipment's strict
        # `{name, available: true}` object convention.
        if isinstance(value, Mapping):
            return _as_strings(value.get("name") or value.get("id") or value.get("value"))
        if isinstance(value, list):
            result: set[str] = set()
            for item in value:
                if isinstance(item, Mapping):
                    result.update(
                        _as_strings(item.get("name") or item.get("id") or item.get("value"))
                    )
                else:
                    result.update(_as_strings(item))
            return result
    return _as_strings(value)


def _available_equipment(profile: Mapping[str, Any]) -> set[str]:
    """Read affirmed gear without combining location-specific inventories."""
    equipment = profile.get("equipment")
    if not isinstance(equipment, list) or not any(isinstance(item, Mapping) for item in equipment):
        return _as_strings(equipment)
    selected = _profile_values(profile, "session_location")
    if len(selected) > 1:
        return set()
    result: set[str] = set()
    for item in equipment:
        if not isinstance(item, Mapping) or item.get("available") is not True:
            continue
        item_locations = _as_strings(item.get("location") or item.get("locations"))
        # Global equipment is usable anywhere.  Location-bound equipment is
        # usable only when the intended session location is explicitly selected.
        if item_locations and (not selected or not item_locations.intersection(selected)):
            continue
        result.update(_as_strings(item.get("name") or item.get("id") or item.get("value")))
    return result


def _partner_known_available(profile: Mapping[str, Any]) -> bool:
    partner = profile.get("partner", profile.get("partners"))
    if isinstance(partner, Mapping):
        availability = partner.get("availability")
        if availability is True:
            return True
        if not isinstance(availability, str):
            return False
        return availability.strip().casefold() in {"available", "yes", "confirmed", "true"}
    if isinstance(partner, list):
        return any(_partner_known_available({"partner": item}) for item in partner)
    return bool(profile.get("partner_available") is True)


def _is_applicable(spec: Mapping[str, Any], profile: Mapping[str, Any]) -> bool:
    equipment = spec.get("equipment", {})
    if not isinstance(equipment, Mapping):
        return False
    available = _available_equipment(profile)
    required_all = _as_strings(equipment.get("required_all"))
    required_any = _as_strings(equipment.get("required_any"))
    if not required_all.issubset(available):
        return False
    if required_any and not required_any.intersection(available):
        return False

    location = spec.get("location", {})
    allowed_locations = _as_strings(
        location.get("allowed") if isinstance(location, Mapping) else []
    )
    stated_locations = _profile_values(profile, "locations") or _profile_values(profile, "location")
    # Location is a compatibility filter when known.  Unlike equipment it is
    # not an implied prerequisite (many source cards are valid indoors/outdoors).
    if (
        allowed_locations
        and stated_locations
        and not allowed_locations.intersection(stated_locations)
    ):
        return False

    eligibility = spec.get("eligibility", {})
    if not isinstance(eligibility, Mapping):
        return False
    for field in eligibility.get("required_profile", []):
        value = profile.get(field)
        if value in (None, "", [], {}, "unknown") and field in {"flash_grade", "onsight_grade"}:
            for experience in profile.get("experience", []):
                if not isinstance(experience, Mapping):
                    continue
                # These are explicit grade fields in TrainingProfileCandidate;
                # a generic activity level never becomes a grade by inference.
                value = experience.get(field)
                if value not in (None, "", "unknown"):
                    break
                if experience.get("activity") == field:
                    value = experience.get("level")
                    if value not in (None, "", "unknown"):
                        break
        if value in (None, "", [], {}, "unknown"):
            return False
    if eligibility.get("requires_partner") and not _partner_known_available(profile):
        return False
    for requirement in eligibility.get("requirements", []):
        if not isinstance(requirement, Mapping):
            return False
        value = profile.get(requirement.get("field"))
        if value in (None, "", [], {}, "unknown"):
            return False
        if "equals" in requirement and value != requirement["equals"]:
            return False
    return True


def _public_protocol(row: CatalogProtocol) -> dict[str, Any]:
    spec = copy.deepcopy(row.specification)
    return {
        "protocol_id": row.id,
        "protocol_version": row.version,
        "slug": row.slug,
        "title": row.title,
        "category": row.category,
        "parameters": spec.get("default_parameters", []),
        "specification": spec,
    }


async def catalog_context(
    session: AsyncSession, *, profile: dict[str, Any], limit: int = 20
) -> list[dict[str, Any]]:
    """Return ready, applicable source protocols in a stable order.

    Specs intentionally contain no private intake identifiers, OCR, raw source
    text, user examples, logged workouts, or athlete data.
    """
    if limit < 0:
        raise ValueError("limit must not be negative")
    rows = (
        await session.scalars(
            select(CatalogProtocol).order_by(CatalogProtocol.slug, CatalogProtocol.version.desc())
        )
    ).all()
    latest: dict[str, CatalogProtocol] = {}
    for row in rows:
        latest.setdefault(row.slug, row)
    # Do not fall back to a previous ready version when the latest source is
    # draft/unready.  The latest row is the authoritative immutable source.
    result = [
        _public_protocol(row)
        for row in sorted(
            latest.values(), key=lambda item: (item.category, item.title, item.version)
        )
        if row.status in _PUBLIC_STATUSES and _is_applicable(row.specification, profile)
    ]
    return result[:limit]


def _parameter_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        return [{"name": key, "value": item} for key, item in value.items()]
    if not isinstance(value, list):
        raise ValueError("parameters must be a list of named values")  # noqa: TRY004 - public API
    return [CatalogParameter.model_validate(item).model_dump(exclude_none=True) for item in value]


def _normalize_ad_hoc_parameters(value: Any) -> list[dict[str, Any]]:
    """One narrow model alias: duration + minutes means duration_minutes."""

    parameters = _parameter_list(value)
    names: set[str] = set()
    for parameter in parameters:
        if parameter["name"] == "duration" and str(parameter.get("unit", "")).casefold() in {
            "min",
            "minute",
            "minutes",
            "мин",
            "минута",
            "минут",
        }:
            parameter["name"] = "duration_minutes"
        if parameter["name"] in names:
            raise ValueError("ad hoc parameters duplicate after duration normalization")
        names.add(parameter["name"])
    return parameters


def _validated_parameters(spec: Mapping[str, Any], supplied: Any) -> list[dict[str, Any]]:
    parameters = _parameter_list(supplied)
    defaults = _parameter_list(spec.get("default_parameters", []))
    default_by_name = {item["name"]: item for item in defaults}
    allowed = {
        item["name"]: item
        for item in spec.get("adjustable_parameters", [])
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    if not parameters:
        return copy.deepcopy(defaults)
    seen: set[str] = set()
    replacements: dict[str, dict[str, Any]] = {}
    additions: list[dict[str, Any]] = []
    for parameter in parameters:
        name = parameter["name"]
        if name in seen:
            raise ValueError(f"parameter {name!r} is duplicated")
        seen.add(name)
        if name in default_by_name and parameter == default_by_name[name]:
            # A planner may repeat the unmodified source defaults in its fixed
            # schema.  That is not an adaptation and is therefore safe.
            replacements[name] = parameter
            continue
        definition = allowed.get(name)
        if definition is None:
            raise ValueError(f"parameter {name!r} is not an approved adjustment")
        unit = definition.get("unit")
        if parameter.get("unit") != unit:
            raise ValueError(f"parameter {name!r} must use unit {unit!r}")
        value = parameter["value"]
        if definition.get("choices") and value not in definition["choices"]:
            raise ValueError(f"parameter {name!r} is outside allowed choices")
        if isinstance(value, bool) and (
            definition.get("minimum") is not None or definition.get("maximum") is not None
        ):
            raise ValueError(f"parameter {name!r} must be numeric")
        if definition.get("minimum") is not None and (
            not isinstance(value, (int, float)) or value < definition["minimum"]
        ):
            raise ValueError(f"parameter {name!r} is below minimum")
        if definition.get("maximum") is not None and (
            not isinstance(value, (int, float)) or value > definition["maximum"]
        ):
            raise ValueError(f"parameter {name!r} is above maximum")
        if name in default_by_name:
            replacements[name] = parameter
        else:
            additions.append(parameter)
    return [replacements.get(item["name"], item) for item in defaults] + additions


def _effective_adaptation(
    spec: Mapping[str, Any], parameters: list[dict[str, Any]], adaptation_reason: Any
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    policy = spec.get("sam_adaptation_policy")
    if not isinstance(policy, Mapping):
        return None, None
    parameter_name = policy["parameter_name"]
    source_count = policy["source_count"]
    selected = next(
        (item.get("value") for item in parameters if item.get("name") == parameter_name),
        source_count,
    )
    if (
        isinstance(selected, bool)
        or not isinstance(selected, int)
        or not policy["minimum"] <= selected <= source_count
    ):
        raise ValueError("Sam volume adaptation must be an integer source-count reduction")
    if selected == source_count:
        return None, None
    if not isinstance(adaptation_reason, str) or not adaptation_reason.strip():
        raise ValueError("Sam volume adaptation needs an adaptation_reason")
    effective_steps = copy.deepcopy(spec["steps"])
    effective_steps[0][policy["root_step_field"]] = selected
    root = effective_steps[0]
    # These selected cards have an exact root work/rest timer; this is a
    # computed timer, not a claim that the source duration itself changed.
    multiplier = root.get("sets", 1) * root.get("reps", 1)
    seconds = root.get("seconds")
    rest_seconds = root.get("rest_seconds")
    if policy["parameter_name"] == "sam_volume_sets":
        following = spec["steps"][1] if len(spec["steps"]) > 1 else {}
        between_rest = (
            following.get("seconds")
            if isinstance(following, Mapping) and following.get("type") == "rest"
            else None
        )
        if isinstance(seconds, int) and isinstance(between_rest, int):
            duration = {
                "kind": "computed_timer",
                "minutes": round(
                    (selected * seconds + max(selected - 1, 0) * between_rest) / 60, 2
                ),
                "basis": "effective sets plus between-set rest; excludes warmup/transitions",
                "source_duration": copy.deepcopy(spec.get("duration", {})),
            }
        else:
            duration = {
                "kind": "source_approximate",
                "source_duration": copy.deepcopy(spec.get("duration", {})),
                "note": "No timer was computed from the source steps.",
            }
    elif isinstance(multiplier, int) and isinstance(seconds, int) and isinstance(rest_seconds, int):
        duration = {
            "kind": "computed_timer",
            "minutes": round(multiplier * (seconds + rest_seconds) / 60, 2),
            "basis": "effective root repeat work plus stated rest",
            "source_duration": copy.deepcopy(spec.get("duration", {})),
        }
    else:
        duration = {
            "kind": "source_approximate",
            "source_duration": copy.deepcopy(spec.get("duration", {})),
            "note": "No timer was computed from the source steps.",
        }
    return effective_steps, duration


async def validate_blocks(
    session: AsyncSession, blocks: list[dict[str, Any]], *, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """Freeze catalog blocks for a plan; never turn a plan into a workout."""
    frozen: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            raise ValueError(f"block {index} must be an object")  # noqa: TRY004 - public API
        protocol_id = block.get("protocol_id", block.get("id", block.get("catalog_protocol_id")))
        if protocol_id is None:
            if block.get("ad_hoc") is not True:
                raise ValueError(f"block {index} is noncatalog; set ad_hoc: true explicitly")
            if not block.get("kind") or not block.get("title"):
                raise ValueError(f"ad hoc block {index} needs kind and title")
            if any(key in block for key in ("protocol_snapshot", "protocol_version", "version")):
                raise ValueError("ad hoc blocks cannot claim a catalog snapshot")
            ad_hoc = {
                "kind": block["kind"],
                "title": block["title"],
                "ad_hoc": True,
                "source": "ad_hoc_noncatalog",
                "parameters": _normalize_ad_hoc_parameters(block.get("parameters")),
            }
            for field in ("instruction", "adaptation_reason"):
                if field in block:
                    if not isinstance(block[field], str) or not block[field].strip():
                        raise ValueError(f"ad hoc block {index} {field} must be non-empty text")
                    ad_hoc[field] = block[field]
            frozen.append(ad_hoc)
            continue
        if not isinstance(protocol_id, int) or isinstance(protocol_id, bool):
            raise ValueError(f"block {index} has invalid protocol_id")  # noqa: TRY004 - public API
        row = await session.get(CatalogProtocol, protocol_id)
        if row is None or row.status not in _PUBLIC_STATUSES:
            raise ValueError(f"block {index} references an unknown or unready protocol")
        requested_version = block.get("protocol_version", block.get("version"))
        if requested_version is not None and requested_version != row.version:
            raise ValueError(f"block {index} references a different protocol version")
        if block.get("title") is not None and block["title"] != row.title:
            raise ValueError(f"block {index} title does not match its protocol")
        if "protocol_snapshot" in block:
            raise ValueError(
                "protocol snapshots are generated by the catalog, not supplied by callers"
            )
        if not _is_applicable(row.specification, profile):
            raise ValueError(f"block {index} protocol is not applicable to this profile")
        parameters = _validated_parameters(row.specification, block.get("parameters"))
        effective_steps, effective_duration = _effective_adaptation(
            row.specification, parameters, block.get("adaptation_reason")
        )
        result = {
            "kind": "catalog_protocol",
            "protocol_id": row.id,
            "protocol_version": row.version,
            "title": row.title,
            "category": row.category,
            "parameters": parameters,
            "protocol_snapshot": {
                "slug": row.slug,
                "version": row.version,
                "title": row.title,
                "category": row.category,
                "specification": copy.deepcopy(row.specification),
            },
        }
        if effective_steps is not None:
            result["effective_steps"] = effective_steps
            result["effective_duration"] = effective_duration
            result["adaptation_reason"] = block["adaptation_reason"].strip()
        frozen.append(result)
    return frozen


def _get(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _private_source_details(message: Any) -> tuple[int, int, str, str | None]:
    athlete_id = _get(message, "athlete_id")
    message_id = _get(message, "id")
    payload = _get(message, "attachment_data", {}) or {}
    payload = payload.get("telegram_payload", payload) if isinstance(payload, Mapping) else {}
    photo = payload.get("photo") if isinstance(payload, Mapping) else None
    if isinstance(photo, list) and photo:
        photo = photo[-1]
    file_unique_id = _get(photo, "file_unique_id") or _get(payload, "file_unique_id")
    album_id = _get(message, "media_group_id") or _get(payload, "media_group_id")
    if (
        not isinstance(athlete_id, int)
        or not isinstance(message_id, int)
        or not isinstance(file_unique_id, str)
    ):
        raise ValueError(  # noqa: TRY004 - public API
            "catalog source needs a persisted athlete message with a photo file_unique_id"
        )
    return athlete_id, message_id, file_unique_id, album_id if isinstance(album_id, str) else None


async def receive_catalog_source(
    session: AsyncSession, *, message: Any, extracted: dict[str, Any] | None = None
) -> CatalogSource:
    """Stage a private photo source idempotently; no publication or workout side effect."""
    athlete_id, message_id, file_unique_id, album_id = _private_source_details(message)
    existing = await session.scalar(
        select(CatalogSource).where(
            CatalogSource.athlete_id == athlete_id,
            CatalogSource.file_unique_id == file_unique_id,
        )
    )
    if existing is not None:
        return existing
    existing_message = await session.scalar(
        select(CatalogSource).where(CatalogSource.source_message_id == message_id)
    )
    if existing_message is not None:
        return existing_message
    source = CatalogSource(
        athlete_id=athlete_id,
        source_message_id=message_id,
        file_unique_id=file_unique_id,
        album_id=album_id,
        status="extracted" if extracted is not None else "received",
        extracted=copy.deepcopy(extracted) if extracted is not None else {},
    )
    session.add(source)
    await session.flush()
    return source
