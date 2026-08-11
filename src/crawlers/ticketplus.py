import re
import sys
import time
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup


API_URL = "https://apis.ticketplus.com.tw/config/api/v1/getS3"
EVENT_URL = "https://ticketplus.com.tw/activity/{event_id}"
USER_AGENT = "concert-agent/0.1 (+local Ticket Plus crawler test)"
REQUEST_TIMEOUT = 30


def _get_json(session: requests.Session, path: str) -> dict[str, Any]:
    response = session.get(API_URL, params={"path": path}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Ticket Plus API 格式異常：{path}")
    return data


def get_event_entries(
    session: requests.Session, limit: int = 5
) -> list[dict[str, str]]:
    data = _get_json(session, "main/mainEvents.json")
    event_ids = data.get("allEventId", [])
    event_info = data.get("allEventMainPageInfo", {})
    if not isinstance(event_ids, list) or not isinstance(event_info, dict):
        raise ValueError("Ticket Plus 活動列表格式異常")

    entries = []
    for event_id in event_ids:
        stable_id = str(event_id).strip()
        summary = event_info.get(stable_id, {})
        if not stable_id or not isinstance(summary, dict) or summary.get("hidden") is True:
            continue
        entries.append(
            {
                "ticketplus_id": stable_id,
                "title": str(summary.get("title") or "").strip(),
                "url": EVENT_URL.format(event_id=stable_id),
            }
        )
        if len(entries) >= limit:
            break
    return entries


def _clean_html(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for unwanted in soup.find_all(
        ["script", "style", "nav", "form", "button", "noscript"]
    ):
        unwanted.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def _as_names(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    names = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("name") or item.get("title") or ""
        name = str(item or "").strip()
        if name:
            names.append(name)
    return names


def _extract_artist(event_data: dict[str, Any], info_html: str) -> str:
    names = []
    for key in ("artist", "artists", "performer", "performers", "cast"):
        if key in event_data:
            names.extend(_as_names(event_data[key]))
    if names:
        return " / ".join(dict.fromkeys(names))

    soup = BeautifulSoup(info_html or "", "html.parser")
    label_pattern = re.compile(
        r"^(?:演出者|藝人|演出陣容|主演|指揮|樂團|Artist|Performer)\s*"
        r"[：:｜|]\s*(.+)$",
        re.IGNORECASE,
    )
    for text in soup.stripped_strings:
        match = label_pattern.match(re.sub(r"\s+", " ", text).strip())
        if not match:
            continue
        candidate = match.group(1).strip(" -–—:：")
        if candidate:
            names.extend(
                part.strip()
                for part in re.split(r"\s*[/／、]\s*", candidate)
                if part.strip()
            )
    return " / ".join(dict.fromkeys(names))


def _extract_genres(event_data: dict[str, Any]) -> list[str]:
    values = []
    for key in ("category", "categories", "tags"):
        if key in event_data:
            values.extend(_as_names(event_data[key]))
    return list(dict.fromkeys(values))


def _parse_session_datetime(session_data: dict[str, Any]) -> str | None:
    date_text = str(session_data.get("date") or "").split("~", 1)[0].strip()
    time_text = str(session_data.get("time") or "").split("~", 1)[0].strip()
    match = re.search(
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2}).*?(\d{1,2}):(\d{2})",
        f"{date_text} {time_text}",
    )
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).isoformat(timespec="seconds")
    except ValueError:
        return None


def _extract_performance_dates(sessions_data: dict[str, Any]) -> list[str]:
    sessions = sessions_data.get("sessions", [])
    if not isinstance(sessions, list):
        return []
    values = {
        parsed
        for session_data in sessions
        if isinstance(session_data, dict) and session_data.get("hidden") is not True
        if (parsed := _parse_session_datetime(session_data)) is not None
    }
    return sorted(values)


def _extract_location(
    event_data: dict[str, Any], sessions_data: dict[str, Any]
) -> tuple[str, str]:
    venue = str(event_data.get("location") or "").strip()
    address = str(event_data.get("address") or "").strip()
    sessions = sessions_data.get("sessions", [])
    if isinstance(sessions, list):
        visible = [
            item
            for item in sessions
            if isinstance(item, dict) and item.get("hidden") is not True
        ]
        if visible:
            venue = venue or str(visible[0].get("location") or "").strip()
            address = address or str(visible[0].get("address") or "").strip()
    city_match = re.search(
        r"(臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|台南市|"
        r"高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|南投縣|"
        r"雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|臺東縣|台東縣|澎湖縣|"
        r"金門縣|連江縣)",
        address,
    )
    return (city_match.group(1) if city_match else "", venue)


def _extract_sale_time(description: str) -> str | None:
    section_match = re.search(
        r"(?:一般販售|正式開賣|全面開賣|啟售|售票時間)(.{0,500})",
        description,
        re.IGNORECASE,
    )
    if not section_match:
        return None
    values = set()
    for match in re.finditer(
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})\s*(\d{1,2}):(\d{2})",
        section_match.group(1),
    ):
        try:
            values.add(
                datetime(*map(int, match.groups())).isoformat(timespec="seconds")
            )
        except ValueError:
            continue
    return min(values) if values else None


def parse_event(
    session: requests.Session, event_id: str, url: str | None = None
) -> dict[str, Any]:
    event_data = _get_json(session, f"event/{event_id}/event.json")
    sessions_data = _get_json(session, f"event/{event_id}/sessions.json")
    info_html = str(event_data.get("info") or "")
    description = _clean_html(info_html)
    city, venue = _extract_location(event_data, sessions_data)
    stable_id = str(event_data.get("event_id") or event_id).strip()

    return {
        "event_id": f"ticketplus_{stable_id}",
        "event_name": str(event_data.get("title") or "").strip(),
        "artist": _extract_artist(event_data, info_html),
        "genres": _extract_genres(event_data),
        "description": description,
        "performance_dates": _extract_performance_dates(sessions_data),
        "city": city,
        "venue": venue,
        "ticket_platform": "TICKETPLUS",
        "sale_time": _extract_sale_time(description),
        "url": url or EVENT_URL.format(event_id=stable_id),
        "first_seen_at": None,
        "last_updated_at": None,
    }


def crawl_entries(
    session: requests.Session,
    entries: list[dict[str, str]],
    request_interval: float = 0.8,
) -> list[dict[str, Any]]:
    events = []
    for index, entry in enumerate(entries):
        if index:
            time.sleep(request_interval)
        try:
            events.append(
                parse_event(
                    session,
                    entry["ticketplus_id"],
                    entry.get("url"),
                )
            )
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            print(
                f"Ticket Plus 活動抓取失敗，已跳過 {entry.get('url', '')}：{error}",
                file=sys.stderr,
            )
    return events


def crawl_ticketplus(limit: int = 5) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "zh-TW,zh;q=0.9",
        }
    )
    entries = get_event_entries(session, limit=limit)
    return crawl_entries(session, entries)
