import json
import sys
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from brain import classify_event
from bridge_jobs import ensure_matched_job, job_path, upsert_bridge_job_content
from content_generator import generate_content
from crawlers.kktix import crawl_kktix
from crawlers.opentix import crawl_opentix
from crawlers.ticketplus import crawl_ticketplus
from storage import (
    EVENTS_FILE,
    get_event,
    has_meaningful_changes,
    initialize_publication_status,
    load_events,
    mark_content_update_needed,
    save_new_event,
    save_publication_content,
    set_event_publication_status,
    update_event,
)
from publish_queue import (
    build_publish_plan,
    load_publish_job,
    load_publish_state,
    record_x_non_success,
    record_x_success,
)
from web_search import research_event
from x_publisher import publish_x
from email_sender import send_email_batch, successful_email_event_ids
from publish_queue import record_email_success


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_LIMITS = {"OPENTIX": 3, "KKTIX": 3, "TICKETPLUS": 3}


def fetch_events() -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, str]]]:
    crawlers: tuple[tuple[str, Callable[..., list[dict[str, Any]]]], ...] = (
        ("OPENTIX", crawl_opentix),
        ("KKTIX", crawl_kktix),
        ("TICKETPLUS", crawl_ticketplus),
    )
    events: list[dict[str, Any]] = []
    fetched = {platform: 0 for platform, _ in crawlers}
    errors: list[dict[str, str]] = []
    for platform, crawler in crawlers:
        try:
            platform_events = crawler(limit=PLATFORM_LIMITS[platform])
            fetched[platform] = len(platform_events)
            events.extend(platform_events)
        except Exception as error:
            errors.append(
                {
                    "ticket_platform": platform,
                    "event_id": "",
                    "error": str(error),
                }
            )
    return events, fetched, errors


def classify_with_research(
    event: dict[str, Any],
    preferences: dict[str, Any],
    classifier: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    first_classification = classifier(event, preferences)
    if first_classification.get("needs_research") is not True:
        return first_classification, False

    research = research_event(event)
    if not research.get("summary") or not research.get("sources"):
        return first_classification, True

    enriched_event = {**event, "research": research}
    return classifier(enriched_event, preferences), True


def process_events(
    events: list[dict[str, Any]],
    preferences: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    counts = {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "matched": 0,
        "unmatched": 0,
        "research": 0,
        "claude_calls": 0,
        "content_calls": 0,
        "jobs_created": 0,
    }
    errors: list[dict[str, str]] = []

    def counted_classifier(
        event: dict[str, Any], user_preferences: dict[str, Any]
    ) -> dict[str, Any]:
        counts["claude_calls"] += 1
        return classify_event(event, user_preferences)

    def counted_content_generator(
        event: dict[str, Any],
        classification: dict[str, Any],
        user_preferences: dict[str, Any],
    ) -> dict[str, Any]:
        counts["claude_calls"] += 1
        counts["content_calls"] += 1
        return generate_content(event, classification, user_preferences)

    for event in events:
        event_id = str(event.get("event_id") or "").strip()
        platform = str(event.get("ticket_platform") or "").strip()
        try:
            old_event = get_event(event_id, state=state)
            if old_event is not None and not has_meaningful_changes(old_event, event):
                counts["unchanged"] += 1
                old_classification = old_event.get("classification", {})
                old_matched = old_classification.get("matched") is True
                initialize_publication_status(event_id, old_matched, state=state)
                needs_job = old_event.get("x_status", "pending") == "pending" or old_event.get(
                    "email_status", "pending"
                ) == "pending"
                if old_matched and needs_job and not job_path(event_id).exists():
                    if old_event.get("x_content") and old_event.get("email_content"):
                        upsert_bridge_job_content(
                            old_event,
                            old_classification,
                            {
                                "x_content": old_event["x_content"],
                                "email_content": old_event["email_content"],
                            },
                        )
                        created = True
                    else:
                        job, created = ensure_matched_job(
                            event,
                            old_classification,
                            preferences,
                            generator=counted_content_generator,
                        )
                        if created and job is not None:
                            save_publication_content(
                                event_id,
                                job["x_content"],
                                job["email_content"],
                                state=state,
                            )
                    if created:
                        counts["jobs_created"] += 1
                continue

            classification, researched = classify_with_research(
                event, preferences, counted_classifier
            )
            if researched:
                counts["research"] += 1

            if old_event is None:
                save_new_event(event, classification, state=state)
                counts["new"] += 1
            else:
                update_event(event, classification, state=state)
                counts["updated"] += 1

            if classification.get("matched") is True:
                counts["matched"] += 1
                if old_event is not None and job_path(event_id).exists():
                    mark_content_update_needed(event_id, state=state)
                else:
                    _, generated = ensure_matched_job(
                        event,
                        classification,
                        preferences,
                        generator=counted_content_generator,
                    )
                    if generated:
                        counts["jobs_created"] += 1
                        job = load_publish_job(event_id)
                        save_publication_content(
                            event_id,
                            job["x_content"],
                            job["email_content"],
                            state=state,
                        )
            else:
                counts["unmatched"] += 1
        except Exception as error:
            errors.append(
                {
                    "event_id": event_id,
                    "ticket_platform": platform,
                    "error": str(error),
                }
            )

    return {**counts, "errors": errors}


def run_agent_once() -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    with (PROJECT_ROOT / "config" / "preferences.json").open(
        encoding="utf-8"
    ) as preferences_file:
        preferences = json.load(preferences_file)

    state = load_events(EVENTS_FILE)
    events, fetched, crawler_errors = fetch_events()
    processing = process_events(events, preferences, state)
    plan = build_publish_plan()
    x_results: list[dict[str, Any]] = []
    x_published = 0
    x_failed = 0
    for planned_job in plan["x_jobs"]:
        event_id = planned_job["event_id"]
        try:
            job = load_publish_job(event_id)
            result = publish_x(job)
            if result.get("success") is True:
                set_event_publication_status(
                    event_id, x_status="published", state=state
                )
                try:
                    record_x_success(event_id)
                except Exception as state_error:
                    result["error"] = (
                        "X 已發布且 events memory 已標記 published，但 queue state 更新失敗："
                        f"{state_error}"
                    )
                x_published += 1
            else:
                record_x_non_success(
                    event_id, str(result.get("queue_status") or "failed")
                )
                set_event_publication_status(
                    event_id,
                    x_status=str(result.get("queue_status") or "failed"),
                    state=state,
                )
                x_failed += 1
            x_results.append(
                {
                    "event_id": event_id,
                    "success": result.get("success") is True,
                    "post_id": result.get("post_id", ""),
                    "post_url": result.get("post_url", ""),
                    "submission_id": result.get("submission_id", ""),
                    "error": result.get("error"),
                }
            )
        except Exception as error:
            try:
                record_x_non_success(event_id, "failed")
                set_event_publication_status(
                    event_id, x_status="failed", state=state
                )
            except Exception:
                pass
            x_failed += 1
            x_results.append(
                {
                    "event_id": event_id,
                    "success": False,
                    "post_id": "",
                    "post_url": "",
                    "submission_id": "",
                    "error": str(error),
                }
            )
    publish_state = load_publish_state()
    email_batch = plan["email_batch"]
    email_event_ids = [item["event_id"] for item in email_batch]
    email_summary: dict[str, Any] = {
        "planned_batches": 1 if email_batch else 0,
        "planned_events": len(email_batch),
        "sent_batches": 0,
        "sent_events": 0,
        "failed": 0,
        "event_ids": email_event_ids,
        "subject": "",
        "recipients": [],
        "outcome": "",
        "error": None,
    }
    if email_batch:
        already_sent = successful_email_event_ids()
        reconciled_ids = [
            event_id for event_id in email_event_ids if event_id in already_sent
        ]
        if reconciled_ids:
            try:
                for event_id in reconciled_ids:
                    set_event_publication_status(
                        event_id, email_status="sent", state=state
                    )
                record_email_success(reconciled_ids)
                email_summary["error"] = (
                    "偵測到既有成功 Email outcome，已只修復本機狀態，未重寄"
                )
            except Exception as error:
                email_summary["failed"] = 1
                email_summary["error"] = str(error)
        else:
            try:
                email_result = send_email_batch(email_batch)
                email_summary["sent_batches"] = 1
                email_summary["sent_events"] = len(email_result["event_ids"])
                email_summary["subject"] = email_result["subject"]
                masked_recipients = []
                for recipient in email_result["recipients"]:
                    local, separator, domain = recipient.partition("@")
                    masked_recipients.append(
                        f"{local[:2] if len(local) > 2 else local[:1]}***@{domain}"
                        if separator
                        else "***"
                    )
                email_summary["recipients"] = masked_recipients
                email_summary["outcome"] = email_result["outcome_path"]
            except Exception as error:
                email_summary["failed"] = 1
                email_summary["error"] = str(error)
            else:
                try:
                    for event_id in email_result["event_ids"]:
                        set_event_publication_status(
                            event_id, email_status="sent", state=state
                        )
                    record_email_success(email_result["event_ids"])
                except Exception as error:
                    email_summary["failed"] = 1
                    email_summary["error"] = (
                        "SMTP 已成功但本機狀態更新失敗；成功 outcome 已保存，"
                        f"下次不得重寄：{error}"
                    )
    publish_state = load_publish_state()
    summary = {
        "run_summary": {
            "fetched": fetched,
            "new": processing["new"],
            "updated": processing["updated"],
            "unchanged": processing["unchanged"],
            "matched": processing["matched"],
            "unmatched": processing["unmatched"],
            "research": processing["research"],
            "claude_calls": processing["claude_calls"],
            "content_calls": processing["content_calls"],
            "jobs_created": processing["jobs_created"],
            "x": {
                "planned": len(plan["x_jobs"]),
                "published": x_published,
                "failed": x_failed,
                "results": x_results,
            },
            "x_published_count": publish_state["x_published_count"],
            "email": email_summary,
            "email_sent_count": publish_state["email_sent_count"],
            "errors": crawler_errors + processing["errors"],
        }
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def regenerate_saved_job_and_maybe_publish_x(event_id: str) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    with (PROJECT_ROOT / "config" / "preferences.json").open(
        encoding="utf-8"
    ) as preferences_file:
        preferences = json.load(preferences_file)
    state = load_events(EVENTS_FILE)
    event = get_event(event_id, state=state)
    if event is None:
        raise ValueError(f"events.json 找不到活動：{event_id}")
    classification = event.get("classification", {})
    if classification.get("matched") is not True:
        raise ValueError("指定活動的 final classification 不是 matched=true")

    generated = generate_content(event, classification, preferences)
    job = upsert_bridge_job_content(event, classification, generated)
    plan = build_publish_plan()
    selected = any(item["event_id"] == event_id for item in plan["x_jobs"])
    publication: dict[str, Any] | None = None
    if selected and job.get("x_status") == "pending":
        publication = publish_x(job)
        if publication.get("success") is True:
            record_x_success(event_id)
        else:
            record_x_non_success(
                event_id, str(publication.get("queue_status") or "failed")
            )
    return {
        "event_id": event_id,
        "x_content": generated["x_content"],
        "x_weighted_length": generated["x_weighted_length"],
        "x_fallback_used": generated["x_fallback_used"],
        "email_content": generated["email_content"],
        "queue_selected": selected,
        "publication": publication,
        "email_status": load_publish_job(event_id).get("email_status"),
        "x_status": load_publish_job(event_id).get("x_status"),
        "x_published_count": load_publish_state()["x_published_count"],
    }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_agent_once()
