import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from content_generator import generate_content


PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = PROJECT_ROOT / "bridge" / "jobs"
OUTCOMES_DIR = PROJECT_ROOT / "bridge" / "outcomes"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def job_path(event_id: str, jobs_dir: Path = JOBS_DIR) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", event_id):
        raise ValueError("event_id 含有不安全的檔名字元")
    return jobs_dir / f"{event_id}.json"


def create_bridge_job(
    event: dict[str, Any],
    classification: dict[str, Any],
    x_content: str,
    email_content: str,
    jobs_dir: Path = JOBS_DIR,
    now: str | None = None,
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or "").strip()
    path = job_path(event_id, jobs_dir)
    if path.exists():
        raise FileExistsError(f"Bridge job 已存在：{event_id}")
    timestamp = now or _now()
    job = {
        "event_id": event_id,
        "event_name": event.get("event_name", ""),
        "ticket_platform": event.get("ticket_platform", ""),
        "url": event.get("url", ""),
        "genres": classification.get("genres", []),
        "classification": {
            key: classification.get(key)
            for key in ("matched", "genres", "relevance_score", "reason")
        },
        "x_content": x_content,
        "email_content": email_content,
        "actions": {"publish_x": True, "send_email": True},
        "x_status": event.get("x_status", "pending"),
        "email_status": event.get("email_status", "pending"),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    _write_job(path, job)
    return job


def _write_job(path: Path, job: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    OUTCOMES_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(job, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def upsert_bridge_job_content(
    event: dict[str, Any],
    classification: dict[str, Any],
    generated: dict[str, Any],
    jobs_dir: Path = JOBS_DIR,
) -> dict[str, Any]:
    event_id = str(event.get("event_id") or "").strip()
    path = job_path(event_id, jobs_dir)
    if not path.exists():
        return create_bridge_job(
            event,
            classification,
            str(generated["x_content"]),
            str(generated["email_content"]),
            jobs_dir=jobs_dir,
        )
    with path.open(encoding="utf-8") as file:
        job = json.load(file)
    if not isinstance(job, dict):
        raise RuntimeError("既有 Bridge job 格式無效")
    job["x_content"] = str(generated["x_content"])
    job["email_content"] = str(generated["email_content"])
    job.pop("content", None)
    job["updated_at"] = _now()
    _write_job(path, job)
    return job


def ensure_matched_job(
    event: dict[str, Any],
    classification: dict[str, Any],
    preferences: dict[str, Any],
    generator: Callable[..., dict[str, Any]] = generate_content,
    jobs_dir: Path = JOBS_DIR,
) -> tuple[dict[str, Any] | None, bool]:
    if classification.get("matched") is not True:
        return None, False
    path = job_path(str(event.get("event_id") or "").strip(), jobs_dir)
    if path.exists():
        return None, False
    generated = generator(event, classification, preferences)
    return create_bridge_job(
        event,
        classification,
        generated["x_content"],
        generated["email_content"],
        jobs_dir=jobs_dir,
    ), True


def test_matched_job_flow() -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    with (PROJECT_ROOT / "config" / "preferences.json").open(
        encoding="utf-8"
    ) as file:
        preferences = json.load(file)
    event = {
        "event_id": "test_lisa_ono_bridge",
        "event_name": "Lisa Ono Bossa Nova Live",
        "artist": "Lisa Ono",
        "genres": [],
        "description": "Lisa Ono 的 Bossa Nova 演出，融合巴西音樂與 Jazz。",
        "performance_dates": ["2026-10-03T19:30:00"],
        "city": "台北市",
        "venue": "測試音樂廳",
        "ticket_platform": "TEST",
        "sale_time": "2026-08-20T12:00:00",
        "url": "https://example.test/lisa-ono",
        "first_seen_at": None,
        "last_updated_at": None,
    }
    classification = {
        "matched": True,
        "needs_research": False,
        "genres": ["Jazz", "Bossa Nova"],
        "relevance_score": 0.95,
        "reason": "Bossa Nova 與 Jazz 高度相關。",
    }
    calls = 0

    def counted_generator(*args: Any, **kwargs: Any) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return generate_content(*args, **kwargs)

    first_job, first_created = ensure_matched_job(
        event, classification, preferences, generator=counted_generator
    )
    first_calls = calls
    _, second_created = ensure_matched_job(
        event, classification, preferences, generator=counted_generator
    )
    result = {
        "first_created": first_created,
        "first_claude_calls": first_calls,
        "second_created": second_created,
        "second_claude_calls": calls - first_calls,
        "job": first_job,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result
