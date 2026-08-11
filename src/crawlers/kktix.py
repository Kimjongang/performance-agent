import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


FEED_URL = "https://kktix.com/events.atom"
USER_AGENT = "concert-agent/0.1 (+local KKTIX crawler test)"
REQUEST_TIMEOUT = 30
ATOM_NAMESPACE = {"atom": "http://www.w3.org/2005/Atom"}
EVENT_ID_PATTERN = re.compile(r"(?:Event|CollectionEvent)/(\d+)$")


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _entry_url(entry: ET.Element) -> str:
    for link in entry.findall("atom:link", ATOM_NAMESPACE):
        if link.get("rel") == "alternate" and link.get("href"):
            return _clean_url(link.get("href", ""))
    return ""


def get_event_entries(session: requests.Session, limit: int = 20) -> list[dict[str, str]]:
    response = session.get(FEED_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    entries = []
    for entry in root.findall("atom:entry", ATOM_NAMESPACE):
        atom_id = entry.findtext("atom:id", default="", namespaces=ATOM_NAMESPACE)
        match = EVENT_ID_PATTERN.search(atom_id)
        url = _entry_url(entry)
        if not match or not url or "/events/" not in urlsplit(url).path:
            continue
        entries.append(
            {
                "kktix_id": match.group(1),
                "title": entry.findtext(
                    "atom:title", default="", namespaces=ATOM_NAMESPACE
                ).strip(),
                "url": url,
            }
        )
        if len(entries) >= limit:
            break
    return entries


def _event_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            value = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        records = value if isinstance(value, list) else [value]
        for record in records:
            if isinstance(record, dict) and record.get("@type") == "Event":
                return record
    raise ValueError("詳細頁沒有可用的 Event JSON-LD")


def _description_container(soup: BeautifulSoup) -> BeautifulSoup | None:
    return soup.select_one(".description")


def _extract_description(soup: BeautifulSoup) -> str:
    container = _description_container(soup)
    if container is None:
        return ""
    for unwanted in container.find_all(
        ["script", "style", "nav", "form", "button", "noscript"]
    ):
        unwanted.decompose()
    return re.sub(r"\s+", " ", container.get_text(" ", strip=True)).strip()


def _performer_names(value: Any) -> list[str]:
    performers = value if isinstance(value, list) else [value]
    names = []
    for performer in performers:
        if isinstance(performer, dict) and performer.get("name"):
            names.append(str(performer["name"]).strip())
        elif isinstance(performer, str) and performer.strip():
            names.append(performer.strip())
    return names


def _extract_artist(event_data: dict[str, Any], soup: BeautifulSoup) -> str:
    names = _performer_names(event_data.get("performer", []))
    if names:
        return " / ".join(dict.fromkeys(names))

    container = _description_container(soup)
    if container is None:
        return ""
    role_pattern = re.compile(
        r"(?:^|[|｜])(?:藝人|演出者|表演者|Performer|Artist|演出陣容|主演|"
        r"指揮|樂團|金曲入圍歌手|創作歌手|歌手|DJ)"
        r"\s*(?:[|｜:：]\s*)?([^\n📍✨|｜]+)",
        re.IGNORECASE,
    )
    for text in container.stripped_strings:
        for match in role_pattern.finditer(text.strip()):
            candidate = match.group(1).strip(" -–—:：")
            candidate = re.split(r"[（(\[]", candidate, maxsplit=1)[0].strip()
            if candidate and not candidate.startswith(("、", "，", ",")):
                names.append(candidate)
    return " / ".join(dict.fromkeys(names))


def _extract_genres(event_data: dict[str, Any]) -> list[str]:
    value = event_data.get("eventType")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _year_from_event(event_data: dict[str, Any]) -> int | None:
    start_date = str(event_data.get("startDate") or "")
    match = re.match(r"(\d{4})-", start_date)
    return int(match.group(1)) if match else None


def _iso_datetime(year: int, month: int, day: int, hour: int, minute: int) -> str:
    return datetime(year, month, day, hour, minute).isoformat(timespec="seconds")


def _extract_dates(event_data: dict[str, Any], description: str) -> list[str]:
    dates = set()
    year = _year_from_event(event_data)
    offers = event_data.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    if year and isinstance(offers, list):
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            name = str(offer.get("name") or "")
            match = re.search(
                r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d).*?"
                r"(?<!\d)(\d{1,2}):(\d{2})",
                name,
            )
            if match:
                month, day, hour, minute = map(int, match.groups())
                try:
                    dates.add(_iso_datetime(year, month, day, hour, minute))
                except ValueError:
                    continue

    for match in re.finditer(
        r"日期\s*[|｜:：]\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
        r".{0,100}?時間\s*[|｜:：]\s*(\d{1,2}):(\d{2})",
        description,
    ):
        values = map(int, match.groups())
        try:
            dates.add(_iso_datetime(*values))
        except ValueError:
            continue

    if dates:
        return sorted(dates)
    start_date = str(event_data.get("startDate") or "").strip()
    if start_date:
        return [start_date[:19]]
    return []


def _extract_location(event_data: dict[str, Any]) -> tuple[str, str]:
    location = event_data.get("location")
    if not isinstance(location, dict):
        return "", ""
    venue = str(location.get("name") or "").strip()
    address = location.get("address")
    if isinstance(address, dict):
        address = address.get("streetAddress") or address.get("addressLocality") or ""
    address_text = str(address or "").strip()
    if "請依活動頁面" in venue:
        venue = ""
    city_match = re.search(
        r"(?:^|\d{3})(臺北市|台北市|新北市|桃園市|臺中市|台中市|臺南市|"
        r"台南市|高雄市|基隆市|新竹市|嘉義市|新竹縣|苗栗縣|彰化縣|"
        r"南投縣|雲林縣|嘉義縣|屏東縣|宜蘭縣|花蓮縣|臺東縣|台東縣|"
        r"澎湖縣|金門縣|連江縣)",
        address_text,
    )
    city = city_match.group(1) if city_match else ""
    return city, venue


def _extract_sale_time(event_data: dict[str, Any]) -> str | None:
    offers = event_data.get("offers", [])
    if isinstance(offers, dict):
        offers = [offers]
    values = {
        str(offer.get("validFrom"))[:19]
        for offer in offers
        if isinstance(offer, dict) and offer.get("validFrom")
    }
    return min(values) if values else None


def parse_event_page(
    session: requests.Session,
    url: str,
    kktix_id: str | None = None,
) -> dict[str, Any]:
    clean_url = _clean_url(url)
    response = session.get(clean_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    event_data = _event_json_ld(soup)
    description = _extract_description(soup)
    city, venue = _extract_location(event_data)
    stable_id = kktix_id or urlsplit(clean_url).path.rsplit("/", 1)[-1]

    return {
        "event_id": f"kktix_{stable_id}",
        "event_name": str(event_data.get("name") or "").strip(),
        "artist": _extract_artist(event_data, soup),
        "genres": _extract_genres(event_data),
        "description": description,
        "performance_dates": _extract_dates(event_data, description),
        "city": city,
        "venue": venue,
        "ticket_platform": "KKTIX",
        "sale_time": _extract_sale_time(event_data),
        "url": clean_url,
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
                parse_event_page(session, entry["url"], entry.get("kktix_id"))
            )
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            print(
                f"KKTIX 活動抓取失敗，已跳過 {entry.get('url', '')}：{error}",
                file=sys.stderr,
            )
    return events


def crawl_kktix(limit: int = 5) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"}
    )
    entries = get_event_entries(session, limit=limit)
    return crawl_entries(session, entries)
