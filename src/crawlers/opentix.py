import json
import re
import sys
import time
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.opentix.life/"
EVENT_PATH_PATTERN = re.compile(r"^/event/(\d+)$")
USER_AGENT = "concert-agent/0.1 (+local OPENTIX crawler test)"
REQUEST_TIMEOUT = 30


def _clean_url(url: str) -> str:
    parts = urlsplit(urljoin(BASE_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _event_id_from_url(url: str) -> str:
    match = EVENT_PATH_PATTERN.match(urlsplit(url).path.rstrip("/"))
    if not match:
        raise ValueError(f"無法從 OPENTIX URL 取得 event ID：{url}")
    return f"opentix_{match.group(1)}"


def _iter_json_ld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not script.string:
            continue
        try:
            value = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        values = value if isinstance(value, list) else [value]
        records.extend(item for item in values if isinstance(item, dict))
    return records


def _find_event_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    for record in _iter_json_ld(soup):
        if record.get("@type") == "Event":
            return record
    raise ValueError("詳細頁沒有可用的 Event JSON-LD")


def _extract_description(soup: BeautifulSoup, fallback: str = "") -> str:
    heading = next(
        (
            item
            for item in soup.find_all(["h1", "h2", "h3", "h4"])
            if "節目介紹" in item.get_text(" ", strip=True)
        ),
        None,
    )
    container = heading.find_parent(class_="content__introduce__wrapper") if heading else None
    if container is None and heading is not None:
        container = heading.parent
    if container is None:
        return re.sub(r"\s+", " ", fallback).strip()

    for unwanted in container.find_all(["script", "style", "nav", "button"]):
        unwanted.decompose()
    text = container.get_text(" ", strip=True)
    text = re.sub(r"^節目介紹\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_artist(event_data: dict[str, Any], soup: BeautifulSoup) -> str:
    performers = event_data.get("performer", [])
    if isinstance(performers, dict):
        performers = [performers]
    names = []
    if isinstance(performers, list):
        for performer in performers:
            if isinstance(performer, dict) and performer.get("name"):
                names.append(str(performer["name"]).strip())
            elif isinstance(performer, str):
                names.append(performer.strip())
    if names:
        return " / ".join(dict.fromkeys(name for name in names if name))

    heading = next(
        (
            item
            for item in soup.find_all(["h1", "h2", "h3", "h4"])
            if "節目介紹" in item.get_text(" ", strip=True)
        ),
        None,
    )
    container = heading.find_parent(class_="content__introduce__wrapper") if heading else None
    if container is None:
        return ""

    role_pattern = re.compile(
        r"^(?P<role>領銜主演|主演|演出者|表演者|演出團隊|演出團體|演出陣容|"
        r"音樂劇菁英演員聯合演出|指揮|鋼琴家|演奏家|歌手|演唱)"
        r"(?:\s+[A-Za-z ]+)?\s*[|｜︱:：]\s*(?P<names>.+)$"
    )
    seen_roles = set()
    for text in container.stripped_strings:
        match = role_pattern.match(text.strip())
        if not match or match.group("role") in seen_roles:
            continue
        seen_roles.add(match.group("role"))
        value = re.split(r"[［\[]", match.group("names"), maxsplit=1)[0].strip()
        names.extend(
            name.strip()
            for name in re.split(r"\s*(?:、|，|,)\s*", value)
            if name.strip()
        )
    return " / ".join(dict.fromkeys(names))


def _extract_genres(soup: BeautifulSoup) -> list[str]:
    page_text = soup.get_text(" ", strip=True)
    match = re.search(r"類別\s*[：:]\s*([^\s]+)", page_text)
    return [match.group(1)] if match else []


def _split_javascript_arguments(arguments: str) -> list[str]:
    values = []
    start = 0
    depth = 0
    quote = None
    escaped = False
    for index, character in enumerate(arguments):
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth -= 1
        elif character == "," and depth == 0:
            values.append(arguments[start:index].strip())
            start = index + 1
    values.append(arguments[start:].strip())
    return values


def _decode_javascript_literal(value: str) -> Any:
    if value == "undefined" or value == "void 0":
        return None
    if value.startswith("Array("):
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _extract_balanced(text: str, start: int, opening: str, closing: str) -> str:
    depth = 0
    quote = None
    escaped = False
    for index in range(start, len(text)):
        character = text[index]
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
        elif character == opening:
            depth += 1
        elif character == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ValueError("Nuxt payload 結構不完整")


def _nuxt_values(script: str) -> tuple[str, dict[str, Any]]:
    header = re.match(r"window\.__NUXT__=\(function\(([^)]*)\)\{", script)
    if not header or not script.endswith("));"):
        raise ValueError("無法解析 Nuxt payload 外層結構")
    function_start = header.end() - 1
    function_body = _extract_balanced(script, function_start, "{", "}")
    invocation_start = function_start + len(function_body)
    if invocation_start >= len(script) or script[invocation_start] != "(":
        raise ValueError("無法定位 Nuxt payload 參數")
    parameters = [item.strip() for item in header.group(1).split(",")]
    arguments = _split_javascript_arguments(script[invocation_start + 1 : -3])
    values = {
        parameter: _decode_javascript_literal(argument)
        for parameter, argument in zip(parameters, arguments)
    }
    return function_body[1:-1], values


def _extract_nuxt_dates(soup: BeautifulSoup) -> list[str]:
    script = next(
        (
            item.string.strip()
            for item in soup.find_all("script")
            if item.string and item.string.lstrip().startswith("window.__NUXT__=")
        ),
        None,
    )
    if not script:
        return []

    body, values = _nuxt_values(script)
    program_marker = body.find("program:{")
    if program_marker < 0:
        return []
    program_start = program_marker + len("program:")
    program = _extract_balanced(body, program_start, "{", "}")
    venue_marker = program.find("eventVenues:[")
    if venue_marker < 0:
        return []
    venues_start = venue_marker + len("eventVenues:")
    venues = _extract_balanced(program, venues_start, "[", "]")

    timestamps: set[int | float] = set()
    position = 0
    while True:
        event_marker = venues.find("events:[", position)
        if event_marker < 0:
            break
        events_start = event_marker + len("events:")
        events = _extract_balanced(venues, events_start, "[", "]")
        for match in re.finditer(r"(?<![A-Za-z])startDateTime:([^,}\]]+)", events):
            token = match.group(1).strip()
            value = values.get(token, _decode_javascript_literal(token))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                timestamps.add(value)
        position = events_start + len(events)

    timezone = ZoneInfo("Asia/Taipei")
    return [
        datetime.fromtimestamp(timestamp, timezone).replace(tzinfo=None).isoformat(timespec="seconds")
        for timestamp in sorted(timestamps)
    ]


def _extract_dates(event_data: dict[str, Any], soup: BeautifulSoup) -> list[str]:
    try:
        dates = _extract_nuxt_dates(soup)
    except ValueError:
        dates = []
    if dates:
        return dates

    start_date = event_data.get("startDate")
    if isinstance(start_date, str) and start_date.strip():
        dates.append(start_date.strip())
    return dates


def _extract_location(event_data: dict[str, Any]) -> tuple[str, str]:
    location = event_data.get("location")
    if not isinstance(location, dict):
        return "", ""
    venue = str(location.get("name") or "").strip()
    address = location.get("address")
    city = ""
    if isinstance(address, dict):
        city = str(address.get("addressLocality") or "").strip()
    return city, venue


def _extract_sale_time(event_data: dict[str, Any]) -> str | None:
    offers = event_data.get("offers")
    if isinstance(offers, list):
        offers = next((item for item in offers if isinstance(item, dict)), None)
    if not isinstance(offers, dict):
        return None
    value = offers.get("availabilityStarts") or offers.get("validFrom")
    return value.strip() if isinstance(value, str) and value.strip() else None


def get_event_urls(session: requests.Session, limit: int = 5) -> list[str]:
    response = session.get(BASE_URL, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    music_urls: list[str] = []
    other_urls: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        url = _clean_url(anchor["href"])
        if not EVENT_PATH_PATTERN.match(urlsplit(url).path.rstrip("/")) or url in seen:
            continue
        seen.add(url)
        anchor_text = anchor.get_text(" ", strip=True)
        target = music_urls if "音樂" in anchor_text else other_urls
        target.append(url)

    candidates = music_urls or other_urls
    return candidates[:limit]


def parse_event_page(session: requests.Session, url: str) -> dict[str, Any]:
    clean_url = _clean_url(url)
    response = session.get(clean_url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    event_data = _find_event_json_ld(soup)
    city, venue = _extract_location(event_data)

    return {
        "event_id": _event_id_from_url(clean_url),
        "event_name": str(event_data.get("name") or "").strip(),
        "artist": _extract_artist(event_data, soup),
        "genres": _extract_genres(soup),
        "description": _extract_description(
            soup,
            str(event_data.get("description") or ""),
        ),
        "performance_dates": _extract_dates(event_data, soup),
        "city": city,
        "venue": venue,
        "ticket_platform": "OPENTIX",
        "sale_time": _extract_sale_time(event_data),
        "url": clean_url,
        "first_seen_at": None,
        "last_updated_at": None,
    }


def crawl_opentix(limit: int = 5, request_interval: float = 0.8) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-TW,zh;q=0.9"})
    urls = get_event_urls(session, limit=limit)
    events = []
    for index, url in enumerate(urls):
        if index:
            time.sleep(request_interval)
        try:
            events.append(parse_event_page(session, url))
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            print(f"OPENTIX 活動抓取失敗，已跳過 {url}：{error}", file=sys.stderr)
    return events
