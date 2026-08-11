import json
import sys
import tempfile
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from brain import classify_event
import requests

from crawlers.opentix import USER_AGENT, parse_event_page
from crawlers.kktix import (
    USER_AGENT as KKTIX_USER_AGENT,
    crawl_entries as crawl_kktix_entries,
    get_event_entries as get_kktix_event_entries,
)
from crawlers.ticketplus import crawl_ticketplus
from storage import (
    EVENTS_FILE,
    get_event,
    has_meaningful_changes,
    load_events,
    save_new_event,
    update_event,
)
from web_search import research_event
from pipeline import run_agent_once


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")

    preferences_path = PROJECT_ROOT / "config" / "preferences.json"
    with preferences_path.open(encoding="utf-8") as preferences_file:
        preferences = json.load(preferences_file)

    event_data = {
        "artist": "小野麗莎",
        "name": "小野麗莎來台演唱會",
        "description": (
            "日本Bossa Nova歌手，融合巴西音樂、"
            "Jazz與流行音樂風格。"
        ),
    }

    try:
        result = classify_event(event_data, preferences)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)


def test_opentix_crawler() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    test_urls = [
        "https://www.opentix.life/event/2067174521020350465",
        "https://www.opentix.life/event/2063125486201606145",
        "https://www.opentix.life/event/2085274590402461697",
    ]
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"}
    )
    for url in test_urls:
        event = parse_event_page(session, url)
        print(json.dumps(event, ensure_ascii=False, indent=2))


def test_opentix_brain() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    load_dotenv(PROJECT_ROOT / ".env")
    preferences_path = PROJECT_ROOT / "config" / "preferences.json"
    with preferences_path.open(encoding="utf-8") as preferences_file:
        preferences = json.load(preferences_file)

    test_urls = [
        "https://www.opentix.life/event/2067174521020350465",
        "https://www.opentix.life/event/2063125486201606145",
        "https://www.opentix.life/event/2085274590402461697",
    ]
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"}
    )

    for url in test_urls:
        try:
            event = parse_event_page(session, url)
        except Exception as error:
            print(
                json.dumps(
                    {"event": {"url": url}, "crawler_error": str(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            continue

        try:
            classification = classify_event(event, preferences)
            output = {"event": event, "classification": classification}
        except Exception as error:
            output = {"event": event, "classification_error": str(error)}
        print(json.dumps(output, ensure_ascii=False, indent=2))


def classify_with_research(
    event: dict,
    preferences: dict,
    classifier: Callable[[dict, dict], dict] = classify_event,
) -> dict:
    first_classification = classifier(event, preferences)
    output = {
        "event": event,
        "first_classification": first_classification,
        "research": None,
        "second_classification": None,
        "final_classification": first_classification,
    }
    if first_classification.get("needs_research") is not True:
        return output

    research = research_event(event)
    output["research"] = research
    if not research.get("summary") or not research.get("sources"):
        return output

    enriched_event = {**event, "research": research}
    second_classification = classifier(enriched_event, preferences)
    output["second_classification"] = second_classification
    output["final_classification"] = second_classification
    return output


def test_research_flow() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv(PROJECT_ROOT / ".env")
    with (PROJECT_ROOT / "config" / "preferences.json").open(
        encoding="utf-8"
    ) as preferences_file:
        preferences = json.load(preferences_file)

    test_events = [
        {
            "event_id": "test_lisa_ono",
            "event_name": "Lisa Ono Live",
            "artist": "Lisa Ono",
            "genres": [],
            "description": "",
            "performance_dates": [],
            "city": "",
            "venue": "",
            "ticket_platform": "TEST",
            "sale_time": None,
            "url": "",
            "first_seen_at": None,
            "last_updated_at": None,
        },
        {
            "event_id": "test_myles_sanko",
            "event_name": "Myles Sanko Live",
            "artist": "Myles Sanko",
            "genres": [],
            "description": "",
            "performance_dates": [],
            "city": "",
            "venue": "",
            "ticket_platform": "TEST",
            "sale_time": None,
            "url": "",
            "first_seen_at": None,
            "last_updated_at": None,
        },
    ]

    for event in test_events:
        result = classify_with_research(event, preferences)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["research"] is not None:
            break


def process_event(
    event: dict,
    preferences: dict,
    state: dict,
    classification_flow: Callable[[dict, dict], dict] = classify_with_research,
    path: Path = EVENTS_FILE,
) -> dict:
    event_id = str(event.get("event_id") or "").strip()
    old_event = get_event(event_id, state=state)
    if old_event is not None and not has_meaningful_changes(old_event, event):
        return {
            "status": "unchanged",
            "event_id": event_id,
            "record": old_event,
            "classification_flow": None,
        }

    flow_result = classification_flow(event, preferences)
    final_classification = flow_result["final_classification"]
    if old_event is None:
        status = "new"
        record = save_new_event(
            event,
            final_classification,
            state=state,
            path=path,
        )
    else:
        status = "updated"
        record = update_event(
            event,
            final_classification,
            state=state,
            path=path,
        )
    return {
        "status": status,
        "event_id": event_id,
        "record": record,
        "classification_flow": flow_result,
    }


def test_storage_and_real_event() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    fake_event = {
        "event_id": "test_storage_event",
        "event_name": "Storage Test Concert",
        "artist": "Test Artist",
        "genres": ["音樂"],
        "description": "A test description.",
        "performance_dates": ["2026-09-01T19:30:00"],
        "city": "臺北市",
        "venue": "Test Venue",
        "ticket_platform": "TEST",
        "sale_time": "2026-08-01T12:00:00",
        "url": "https://example.test/event/1",
        "first_seen_at": None,
        "last_updated_at": None,
    }
    fake_classification = {
        "matched": False,
        "needs_research": False,
        "genres": [],
        "relevance_score": 0.0,
        "reason": "storage test",
    }

    with tempfile.TemporaryDirectory() as directory:
        test_path = Path(directory) / "events.json"
        test_state = load_events(test_path)
        test_a_status = "new" if get_event(fake_event["event_id"], test_state) is None else "unexpected"
        saved = save_new_event(
            fake_event,
            fake_classification,
            state=test_state,
            path=test_path,
            now="2026-08-11T10:00:00Z",
        )
        test_b_status = (
            "unchanged"
            if not has_meaningful_changes(saved, fake_event)
            else "unexpected"
        )
        changed_event = {
            **fake_event,
            "sale_time": "2026-08-02T12:00:00",
        }
        test_c_status = (
            "updated"
            if has_meaningful_changes(saved, changed_event)
            else "unexpected"
        )
        updated = update_event(
            changed_event,
            fake_classification,
            state=test_state,
            path=test_path,
            now="2026-08-11T11:00:00Z",
        )
        print(
            json.dumps(
                {
                    "storage_tests": {
                        "A": test_a_status,
                        "B": test_b_status,
                        "C": test_c_status,
                        "first_seen_preserved": (
                            updated["first_seen_at"] == saved["first_seen_at"]
                        ),
                        "last_updated_changed": (
                            updated["last_updated_at"] != saved["last_updated_at"]
                        ),
                    }
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    load_dotenv(PROJECT_ROOT / ".env")
    with (PROJECT_ROOT / "config" / "preferences.json").open(
        encoding="utf-8"
    ) as preferences_file:
        preferences = json.load(preferences_file)

    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"}
    )
    event = parse_event_page(
        session,
        "https://www.opentix.life/event/2063125486201606145",
    )
    claude_calls = 0

    def counted_classifier(event_data: dict, user_preferences: dict) -> dict:
        nonlocal claude_calls
        claude_calls += 1
        return classify_event(event_data, user_preferences)

    def counted_flow(event_data: dict, user_preferences: dict) -> dict:
        return classify_with_research(
            event_data,
            user_preferences,
            classifier=counted_classifier,
        )

    state = load_events()
    before_first = claude_calls
    first_result = process_event(event, preferences, state, counted_flow)
    first_calls = claude_calls - before_first
    before_second = claude_calls
    second_result = process_event(event, preferences, state, counted_flow)
    second_calls = claude_calls - before_second
    print(
        json.dumps(
            {
                "real_event_test": {
                    "first": first_result,
                    "first_claude_calls": first_calls,
                    "second": second_result,
                    "second_claude_calls": second_calls,
                    "total_claude_calls": claude_calls,
                    "state_file": str(EVENTS_FILE),
                }
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def test_kktix_crawler() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": KKTIX_USER_AGENT,
            "Accept-Language": "zh-TW,zh;q=0.9",
        }
    )
    entries = get_kktix_event_entries(session, limit=20)
    sample_indices = (0, 8, 13)
    samples = [entries[index] for index in sample_indices if index < len(entries)]
    events = crawl_kktix_entries(session, samples)
    for event in events:
        print(json.dumps(event, ensure_ascii=False, indent=2))


def test_ticketplus_crawler() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for event in crawl_ticketplus(limit=3):
        print(json.dumps(event, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    run_agent_once()
