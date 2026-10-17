"""Deterministic, model-facing projection for the task-execution main agent.

The projection deliberately hides persistence and concurrency metadata without
changing the complete snapshot used by professional capabilities or Store.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
import json
from typing import Any, Iterable, Mapping

from .agent_protocol import PlanBasis, plan_basis
from .plan_display import dates_from_snapshot, text_hash, validate_display
from .planning import required_information
from .task_context import OPEN_TASK_STATES, basis_matches, normalize_workflow


_MISSING = object()


def _get(value: Any, key: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
    elif hasattr(value, key):
        return getattr(value, key)
    if default is _MISSING:
        return None
    return default


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _message_id(message: Any) -> str:
    value = _get(message, "id", "")
    return value if isinstance(value, str) else ""


def _message_status(message: Any) -> str:
    return _get(message, "status", "sent") or "sent"


def project_requirements(trip: Any, missing: Iterable[str] | None = None) -> dict[str, Any]:
    """Project normalized requirements without dropping meaningful falsy values."""
    profile = _get(trip, "requirements", None)
    known: dict[str, Any] = {}
    if profile is not None:
        raw = asdict(profile) if is_dataclass(profile) else dict(profile)
        raw.pop("revision", None)
        # Keep every normalized field, including empty lists, 0 and False.  The
        # current model has no provenance bit for an explicit clear, so omitting
        # empty values would make a clear indistinguishable from an unknown.
        known.update(deepcopy(raw))

    destination = _text(_get(trip, "destination", ""))
    note = _text(_get(trip, "note", ""))
    style = _text(_get(trip, "style", ""))
    if destination:
        known["destination"] = destination
    if note:
        known["note"] = note
    if style and not known.get("pace"):
        known["style"] = style
    # Preserve legacy-only fields when the normalized profile does not carry
    # an equivalent value. Do not overwrite a known normalized field.
    if not (_text(_get(profile, "start_date", "")) and _text(_get(profile, "end_date", ""))):
        legacy_dates = _text(_get(trip, "date_range", ""))
        if legacy_dates:
            known["date_range"] = legacy_dates
    if not _text(_get(profile, "budget_amount", "")):
        legacy_budget = _text(_get(trip, "budget", ""))
        if legacy_budget:
            known["budget"] = legacy_budget

    lodging = _get(trip, "lodging", {}) or {}
    if isinstance(lodging, Mapping):
        lodging_view = {
            "name": lodging.get("name") or None,
            "address": _text(_get(profile, "lodging_address", "")) or lodging.get("address") or None,
            "city": _text(_get(profile, "city", "")) or lodging.get("city") or None,
            "location_confirmed": bool(lodging and lodging.get("poi_id") and _text(_get(profile, "city", ""))),
        }
        if any(value is not None and value is not False for value in lodging_view.values()):
            known["lodging"] = lodging_view

    return {
        "known": known,
        "missing_for_generation": list(missing if missing is not None else required_information(trip)),
    }


def _snapshot_dates(plan: Any) -> list[str] | None:
    snapshot = _get(plan, "requirements_snapshot", {}) or {}
    if not isinstance(snapshot, Mapping):
        return None
    try:
        return dates_from_snapshot(snapshot)
    except (TypeError, ValueError):
        return None


def _valid_display(plan: Any, dates: list[str] | None) -> list[dict[str, Any]] | None:
    # Without a complete saved date window, display day indexes cannot be
    # proven to correspond to the current plan and are not a safe outline.
    if dates is None:
        return None
    if (_get(plan, "display_status", "") != "ready"
            or not isinstance(_get(plan, "display", None), Mapping)
            or _get(plan, "display_source_hash", "") != text_hash(_get(plan, "text", ""))):
        return None
    raw = deepcopy(_get(plan, "display", None))
    try:
        validated = validate_display(raw, dates)
    except (TypeError, ValueError, KeyError):
        return None
    days = validated.get("days") if isinstance(validated, Mapping) else None
    if not isinstance(days, list):
        return None
    structured_days = _get(plan, "days", []) or []
    # Display is index-bound to the structured plan in the UI. Without that
    # source, a valid-looking derivative cannot safely become an outline.
    if not isinstance(structured_days, list) or not structured_days or len(days) != len(structured_days):
        return None
    for index, day in enumerate(days):
        display_items = _get(day, "items", None)
        structured_items = _get(structured_days[index], "items", None)
        if (not isinstance(display_items, list) or not isinstance(structured_items, (list, tuple))
                or len(display_items) != len(structured_items)):
            return None
    return days


def _outline_from_days(days: Any, dates: list[str] | None) -> list[dict[str, Any]] | None:
    if not isinstance(days, list) or not days:
        return None
    outline = []
    for index, day in enumerate(days):
        label = _text(_get(day, "label", "")) or f"第{index + 1}天"
        day_date = _text(_get(day, "date", "")) or (dates[index] if dates and index < len(dates) else None)
        theme = _text(_get(day, "theme", "")) or None
        day_ref = _text(_get(day, "id", "")) or f"day-{index + 1}"
        outline.append({"ref": day_ref, "label": label, "date": day_date, "theme": theme})
    return outline


def project_plan(plan: Any, expected_basis: PlanBasis | Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return a version-bound outline, never a prose prefix masquerading as one."""
    if plan is None or not bool(_get(plan, "has_content", False)):
        return None
    dates = _snapshot_dates(plan)
    outline = None
    basis_matches_plan = True
    if expected_basis is None:
        outline = _outline_from_days(_get(plan, "days", []), dates)
    else:
        try:
            basis_matches_plan = plan_basis(plan) == PlanBasis.from_value(expected_basis)
            if basis_matches_plan:
                outline = _outline_from_days(_get(plan, "days", []), dates)
        except (TypeError, ValueError):
            basis_matches_plan = False
    # A display derivative is only a fallback when the current plan basis is
    # valid; never splice display and plan arrays by index.
    if outline is None and basis_matches_plan:
        display_days = _valid_display(plan, dates)
        outline = _outline_from_days(display_days, dates)
    return {
        "ref": _text(_get(plan, "id", "")),
        "title": _text(_get(plan, "title", "")),
        "status": _text(_get(plan, "status", "")),
        "outline": outline,
    }


def project_conversation(messages: Iterable[Any], current_message_id: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """Deduplicate by server message ID, never by text."""
    selected = []
    positions: dict[str, int] = {}
    for message in messages:
        if _message_status(message) != "sent":
            continue
        ref = _message_id(message)
        if not ref or ref == current_message_id:
            continue
        item = {"ref": ref, "role": _get(message, "sender_type", ""), "text": _get(message, "body", "")}
        if ref in positions:
            selected[positions[ref]] = item
        else:
            positions[ref] = len(selected)
            selected.append(item)
    return selected[-limit:]


def project_capability_observation(result: Any) -> dict[str, Any]:
    """Keep the structured facts main needs, omitting empty optional fields."""
    status = _get(result, "status", "")
    status = status.value if hasattr(status, "value") else status
    output = {"status": status, "summary": _get(result, "summary", "")}
    for key in ("artifact_refs", "evidence_refs", "completed_items", "unresolved_items"):
        values = _get(result, key, ())
        if values:
            output[key] = list(values)
    question = _get(result, "input_request", None)
    if question is not None:
        output["input_request"] = {
            "ref": _get(question, "id", ""),
            "task_ref": _get(question, "conversation_task_id", ""),
            "text": _get(question, "text", ""),
            "answer_target": deepcopy(_get(question, "answer_target", {})),
        }
    error = _get(result, "error", None)
    if error:
        output["error"] = error
    return output


_READ_LIMIT = 20
_MAX_READ_LIMIT = 100
_MAX_TEXT = 4000


def _page_args(raw: Mapping[str, Any] | None) -> tuple[str, int, int]:
    raw = raw if isinstance(raw, Mapping) else {}
    query = raw.get("query", "")
    offset = raw.get("offset", 0)
    limit = raw.get("limit", _READ_LIMIT)
    if not isinstance(query, str):
        raise ValueError("检索词必须为文本")
    if type(offset) is not int or offset < 0:
        raise ValueError("分页偏移无效")
    if type(limit) is not int or not 1 <= limit <= _MAX_READ_LIMIT:
        raise ValueError("分页大小无效")
    return query, offset, limit


def _paged(items: list[dict[str, Any]], *, query: str, offset: int, limit: int) -> dict[str, Any]:
    end = offset + limit
    return {
        "items": deepcopy(items[offset:end]),
        "total": len(items),
        "next_offset": end if end < len(items) else None,
        "query": query,
    }


def project_messages(raw: Any, *, current_message_id: str = "", query: str = "",
                     offset: int = 0, limit: int = _READ_LIMIT) -> dict[str, Any]:
    """Project messages to opaque refs and decision text only."""
    values = raw if isinstance(raw, (list, tuple)) else []
    selected = []
    positions: dict[str, int] = {}
    needle = query.casefold()
    for message in values:
        if _message_status(message) != "sent":
            continue
        ref = _message_id(message)
        if not ref or ref == current_message_id:
            continue
        item = {"ref": ref, "role": _get(message, "sender_type", ""),
                "text": _get(message, "body", "")}
        if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        if ref in positions:
            selected[positions[ref]] = item
        else:
            positions[ref] = len(selected)
            selected.append(item)
    return _paged(selected, query=query, offset=offset, limit=limit)


def project_ideas(raw: Any, *, query: str = "", offset: int = 0,
                  limit: int = _READ_LIMIT) -> dict[str, Any]:
    """Project idea cards while omitting UI-only presentation fields."""
    values = raw if isinstance(raw, (list, tuple)) else []
    selected = []
    needle = query.casefold()
    for idea in values:
        ref = _text(_get(idea, "id", ""))
        if not ref:
            continue
        item = {"ref": ref, "kind": _get(idea, "kind", ""),
                "title": _get(idea, "title", ""), "body": _get(idea, "body", ""),
                "status": _get(idea, "status", ""), "author": _get(idea, "author", "")}
        if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        selected.append(item)
    return _paged(selected, query=query, offset=offset, limit=limit)


def _clip_text(value: Any, maximum: int = _MAX_TEXT) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def _fact_value(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def _allowlisted_facts(tool: str, result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {}
    fields = {
        "query_weather": ("city", "requested_date", "condition", "temperature_max_c",
                          "temperature_min_c", "precipitation_probability_max"),
        "query_route": ("origin_name", "destination_name", "mode", "status",
                        "duration_minutes", "distance_meters", "reason"),
        "search_places": ("city", "status", "reason", "total_matched", "note"),
        "search_food": ("city", "status", "reason", "total_matched", "note"),
    }.get(tool, ("status", "reason"))
    facts = {}
    for key in fields:
        value = _fact_value(result.get(key))
        if value is not None:
            facts[key] = value
    if tool == "search_places" and isinstance(result.get("candidates"), list):
        facts["candidates"] = [
            {key: _fact_value(candidate.get(key)) for key in
             ("name", "address", "city", "district", "place_type", "opening_hours")
             if _fact_value(candidate.get(key)) is not None}
            for candidate in result["candidates"][:5] if isinstance(candidate, Mapping)
        ]
    if tool == "search_food" and isinstance(result.get("recommendations"), list):
        facts["recommendations"] = [
            {key: _fact_value(candidate.get(key)) for key in
             ("name", "rating", "cost_per_person_cny", "address", "district",
              "business_area", "cuisine_tags", "open_time")
             if _fact_value(candidate.get(key)) is not None}
            for candidate in result["recommendations"][:5] if isinstance(candidate, Mapping)
        ]
    return facts


def _project_evidence_row(row: Any, *, ref: str | None = None,
                          evidence_kind: str = "other", source: str = "") -> dict[str, Any] | None:
    if not isinstance(row, Mapping):
        return None
    artifact_payload = row.get("_artifact_payload")
    if isinstance(artifact_payload, Mapping):
        results = artifact_payload.get("results")
        result_rows = results if isinstance(results, (list, tuple)) else []
        fact_rows = []
        statuses = []
        for result_row in result_rows:
            if not isinstance(result_row, Mapping):
                continue
            tool = result_row.get("tool") if isinstance(result_row.get("tool"), str) else ""
            result = result_row.get("result") if isinstance(result_row.get("result"), Mapping) else {}
            facts = _allowlisted_facts(tool, result)
            if facts:
                fact_rows.append({"tool": tool or "other", "facts": facts})
            status = result.get("status")
            if isinstance(status, str):
                statuses.append(status)
        status = statuses[0] if statuses and all(value == statuses[0] for value in statuses) else (
            "mixed" if statuses else "unknown"
        )
        item = {
            "ref": ref,
            "kind": artifact_payload.get("evidence_kind") or evidence_kind,
            "source": source or row.get("_source_capability") or "research",
            "summary": _clip_text(artifact_payload.get("summary", "")),
            "status": status,
            "facts": {"results": fact_rows} if fact_rows else {},
        }
        return {key: value for key, value in item.items() if value not in (None, "", {})}

    tool = row.get("tool") if isinstance(row.get("tool"), str) else ""
    result = row.get("result") if isinstance(row.get("result"), Mapping) else row
    item = {"ref": ref or row.get("_artifact_ref"), "kind": evidence_kind, "source": source or tool or None,
            "summary": _clip_text(row.get("summary", "")),
            "status": result.get("status", "unknown"),
            "facts": _allowlisted_facts(tool, result)}
    if not item["summary"] and isinstance(row.get("reason"), str):
        item["summary"] = _clip_text(row["reason"])
    return {key: value for key, value in item.items() if value not in (None, "", {})}


def project_evidence(raw: Any, *, query: str = "", offset: int = 0,
                     limit: int = _READ_LIMIT) -> dict[str, Any]:
    """Project evidence into bounded, non-authoritative decision facts."""
    values = raw if isinstance(raw, (list, tuple)) else []
    selected = []
    needle = query.casefold()
    for row in values:
        row_ref = _get(row, "_artifact_ref", None)
        item = _project_evidence_row(row, ref=row_ref if isinstance(row_ref, str) else None,
                                     source=_get(row, "_source_capability", ""))
        if item is None:
            continue
        if needle and needle not in json.dumps(item, ensure_ascii=False).casefold():
            continue
        selected.append(item)
    return _paged(selected, query=query, offset=offset, limit=limit)


def project_main_read_context(section: str, raw: Any, *, trip: Any = None, plan: Any = None,
                              expected_basis: PlanBasis | Mapping[str, Any] | None = None,
                              current_message_id: str = "", request_args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Project a non-task main read without changing shared read contracts."""
    query, offset, limit = _page_args(request_args if request_args is not None else raw)
    if section == "requirements":
        return project_requirements(trip, required_information(trip))
    if section == "messages":
        source = _get(raw, "items", []) if isinstance(raw, Mapping) else raw
        return project_messages(source, current_message_id=current_message_id,
                                query=query, offset=offset, limit=limit)
    if section == "ideas":
        source = _get(raw, "items", []) if isinstance(raw, Mapping) else raw
        return project_ideas(source, query=query, offset=offset, limit=limit)
    if section == "plan":
        return project_plan(plan, expected_basis) or {"plan": None}
    if section == "evidence":
        source = _get(raw, "results", []) if isinstance(raw, Mapping) else raw
        return project_evidence(source, query=query, offset=offset, limit=limit)
    raise ValueError("未知上下文类型")


def project_tasks(workflow: Mapping[str, Any], *, message_ids: Iterable[str],
                  plan_basis_value: PlanBasis | Mapping[str, Any] | None,
                  requirements_revision: int | None) -> dict[str, Any]:
    """Expose open task semantics while retaining server-side validity checks."""
    normalized = normalize_workflow(
        workflow, messages=message_ids, plan_basis=plan_basis_value,
        requirements_revision=requirements_revision,
    )
    current_basis = PlanBasis.from_value(plan_basis_value) if plan_basis_value is not None else None
    open_tasks = []
    for task in normalized["tasks"]:
        if task["status"] not in OPEN_TASK_STATES:
            continue
        stale = not basis_matches(task.get("basis"), current_basis, requirements_revision)
        item = {
            "ref": task["id"], "goal": task["goal"], "effect_kind": task["effect_kind"],
            "status": task["status"], "status_reason": task.get("status_reason", ""),
            "progress": deepcopy(task.get("progress", [])),
            "unresolved_items": deepcopy(task.get("unresolved_items", [])),
        }
        if stale:
            item["status"] = "suspended"
            item["requires_reconfirmation"] = True
            item["status_reason"] = "需求或当前行程依据已变化，需要重新确认"
        open_tasks.append(item)

    active = normalized.get("active_question")
    question = None
    if isinstance(active, Mapping):
        question = {
            "ref": active["id"], "task_ref": active["task_id"],
            "text": active["text"], "answer_target": deepcopy(active["answer_target"]),
            "source_message_refs": list(active.get("source_message_ids", [])),
            "assistant_message_ref": active.get("assistant_message_id"),
        }
    focus = normalized.get("focus_task_id")
    if focus not in {item["ref"] for item in open_tasks}:
        focus = None
    return {"focus_task_ref": focus, "open_tasks": open_tasks, "active_question": question}


def project_main_context(*, request_context: Any, execution_context: Any,
                         workflow: Mapping[str, Any], recent_chat: Iterable[Any],
                         plan: Any, missing_requirements: Iterable[str] | None = None) -> dict[str, Any]:
    """Build the six-block main view without changing the execution snapshot."""
    trip = execution_context.trip
    messages = list(execution_context.messages)
    message_ids = [_message_id(message) for message in messages if _message_id(message)]
    return {
        "message": {"ref": request_context.message_id, "text": request_context.body},
        "environment": {
            "current_date": execution_context.current_date,
            "mode": _get(trip, "mode", "travel"),
            "entry": request_context.entry,
        },
        "requirements": project_requirements(trip, missing_requirements),
        "current_plan": project_plan(plan, request_context.plan_basis),
        "conversation": project_conversation(recent_chat, request_context.message_id),
        "task_context": project_tasks(
            workflow, message_ids=message_ids,
            plan_basis_value=request_context.plan_basis,
            requirements_revision=request_context.requirements_revision,
        ),
    }


__all__ = [
    "project_capability_observation", "project_conversation", "project_ideas", "project_evidence",
    "project_main_context", "project_main_read_context", "project_messages", "project_plan",
    "project_requirements", "project_tasks",
]
