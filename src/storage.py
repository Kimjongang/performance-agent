import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENTS_FILE = PROJECT_ROOT / "bridge" / "memory" / "events.json"

EVENT_FIELDS = (
    "event_id",
    "event_name",
    "artist",
    "genres",
    "description",
    "performance_dates",
    "city",
    "venue",
    "ticket_platform",
    "sale_time",
    "url",
)

MEANINGFUL_FIELDS = (
    "event_name",
    "artist",
    "genres",
    "performance_dates",
    "city",
    "venue",
    "sale_time",
    "description",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def empty_state() -> dict[str, Any]:
    return {"version": 1, "events": {}}


def load_events(path: Path = EVENTS_FILE) -> dict[str, Any]:
    if not path.exists():
        return empty_state()
    try:
        with path.open(encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法讀取活動狀態：{error}") from None
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        raise RuntimeError("活動狀態格式無效")
    return state


def save_events(state: dict[str, Any], path: Path = EVENTS_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def get_event(
    event_id: str,
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
) -> dict[str, Any] | None:
    current_state = state if state is not None else load_events(path)
    return current_state["events"].get(event_id)


def _normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalized_field(field: str, value: Any) -> Any:
    if field in {"genres", "performance_dates"}:
        if not isinstance(value, list):
            return []
        return sorted({_normalize_text(item) for item in value if _normalize_text(item)})
    return _normalize_text(value)


def has_meaningful_changes(
    old_event: dict[str, Any],
    new_event: dict[str, Any],
) -> bool:
    return any(
        _normalized_field(field, old_event.get(field))
        != _normalized_field(field, new_event.get(field))
        for field in MEANINGFUL_FIELDS
    )


def _record(
    event: dict[str, Any],
    classification: dict[str, Any],
    first_seen_at: str,
    last_updated_at: str,
) -> dict[str, Any]:
    record = {field: event.get(field) for field in EVENT_FIELDS}
    record["first_seen_at"] = first_seen_at
    record["last_updated_at"] = last_updated_at
    record["classification"] = classification
    matched = classification.get("matched") is True
    record["x_status"] = "pending" if matched else "not_applicable"
    record["email_status"] = "pending" if matched else "not_applicable"
    record["content_update_needed"] = False
    return record


def save_new_event(
    event: dict[str, Any],
    classification: dict[str, Any],
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
    now: str | None = None,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("event_id 不可為空")
    if event_id in current_state["events"]:
        raise ValueError(f"活動已存在：{event_id}")
    timestamp = now or utc_now()
    record = _record(event, classification, timestamp, timestamp)
    current_state["events"][event_id] = record
    save_events(current_state, path)
    return record


def update_event(
    event: dict[str, Any],
    classification: dict[str, Any],
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
    now: str | None = None,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    event_id = str(event.get("event_id") or "").strip()
    old_record = current_state["events"].get(event_id)
    if old_record is None:
        raise ValueError(f"活動不存在：{event_id}")
    record = _record(
        event,
        classification,
        old_record["first_seen_at"],
        now or utc_now(),
    )
    if classification.get("matched") is True:
        record["x_status"] = old_record.get("x_status", "pending")
        record["email_status"] = old_record.get("email_status", "pending")
        record["content_update_needed"] = old_record.get(
            "content_update_needed", False
        )
    current_state["events"][event_id] = record
    save_events(current_state, path)
    return record


def mark_content_update_needed(
    event_id: str,
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    record = current_state["events"].get(event_id)
    if record is None:
        raise ValueError(f"活動不存在：{event_id}")
    record["content_update_needed"] = True
    save_events(current_state, path)
    return record


def initialize_publication_status(
    event_id: str,
    matched: bool,
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    record = current_state["events"].get(event_id)
    if record is None:
        raise ValueError(f"活動不存在：{event_id}")
    default_status = "pending" if matched else "not_applicable"
    changed = any(
        key not in record
        for key in ("x_status", "email_status", "content_update_needed")
    )
    record.setdefault("x_status", default_status)
    record.setdefault("email_status", default_status)
    record.setdefault("content_update_needed", False)
    if changed:
        save_events(current_state, path)
    return record


def save_publication_content(
    event_id: str,
    x_content: str,
    email_content: str,
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    record = current_state["events"].get(event_id)
    if record is None:
        raise ValueError(f"活動不存在：{event_id}")
    record["x_content"] = x_content
    record["email_content"] = email_content
    save_events(current_state, path)
    return record


def set_event_publication_status(
    event_id: str,
    *,
    x_status: str | None = None,
    email_status: str | None = None,
    state: dict[str, Any] | None = None,
    path: Path = EVENTS_FILE,
) -> dict[str, Any]:
    current_state = state if state is not None else load_events(path)
    record = current_state["events"].get(event_id)
    if record is None:
        raise ValueError(f"活動不存在：{event_id}")
    if x_status is not None:
        record["x_status"] = x_status
    if email_status is not None:
        record["email_status"] = email_status
    save_events(current_state, path)
    return record
