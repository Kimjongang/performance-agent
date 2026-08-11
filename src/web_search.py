import re
import time
from typing import Any

import requests


MUSICBRAINZ_SEARCH_URL = "https://musicbrainz.org/ws/2/artist/"
WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "concert-agent/0.1 (music genre research; local project)"
REQUEST_TIMEOUT = 20
MAX_WIKIPEDIA_CHARS = 800
MAX_SUMMARY_CHARS = 1400


def _research_query(event: dict[str, Any]) -> str:
    artist = str(event.get("artist") or "").strip()
    if artist:
        return artist
    return str(event.get("event_name") or "").strip()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _musicbrainz_research(
    session: requests.Session,
    query: str,
) -> tuple[str, dict[str, str] | None]:
    search_response = session.get(
        MUSICBRAINZ_SEARCH_URL,
        params={"query": f'artist:"{query}"', "fmt": "json", "limit": 3},
        timeout=REQUEST_TIMEOUT,
    )
    search_response.raise_for_status()
    artists = search_response.json().get("artists", [])
    candidate = next(
        (
            artist
            for artist in artists
            if int(artist.get("score", 0)) >= 80 and artist.get("id")
        ),
        None,
    )
    if not candidate:
        return "", None

    time.sleep(1.05)
    artist_id = candidate["id"]
    detail_response = session.get(
        f"{MUSICBRAINZ_SEARCH_URL}{artist_id}",
        params={"inc": "genres+tags", "fmt": "json"},
        timeout=REQUEST_TIMEOUT,
    )
    detail_response.raise_for_status()
    details = detail_response.json()

    genres = [
        item["name"]
        for item in details.get("genres", [])
        if item.get("name")
    ][:12]
    tags = [
        item["name"]
        for item in sorted(
            details.get("tags", []),
            key=lambda item: item.get("count", 0),
            reverse=True,
        )
        if item.get("name")
    ][:12]
    background = _clean_text(str(details.get("disambiguation") or ""))
    parts = [f"MusicBrainz artist: {details.get('name', query)}."]
    if background:
        parts.append(f"Background: {background}.")
    if genres:
        parts.append(f"Genres: {', '.join(genres)}.")
    if tags:
        parts.append(f"Community tags: {', '.join(tags)}.")
    if not background and not genres and not tags:
        return "", None
    return " ".join(parts), {
        "title": f"MusicBrainz: {details.get('name', query)}",
        "url": f"https://musicbrainz.org/artist/{artist_id}",
    }


def _wikipedia_research(
    session: requests.Session,
    query: str,
) -> tuple[str, dict[str, str] | None]:
    response = session.get(
        WIKIPEDIA_API_URL,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrlimit": 1,
            "prop": "extracts|info",
            "exintro": 1,
            "explaintext": 1,
            "inprop": "url",
            "format": "json",
            "formatversion": 2,
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    pages = response.json().get("query", {}).get("pages", [])
    if not pages:
        return "", None
    page = pages[0]
    page_title = str(page.get("title") or "").strip()
    normalized_query = _normalized_name(query)
    normalized_title = _normalized_name(page_title)
    if not normalized_query or not (
        normalized_query in normalized_title or normalized_title in normalized_query
    ):
        return "", None
    extract = _clean_text(str(page.get("extract") or ""))[:MAX_WIKIPEDIA_CHARS]
    full_url = str(page.get("fullurl") or "").strip()
    if not extract or not full_url:
        return "", None
    return f"Wikipedia: {extract}", {
        "title": f"Wikipedia: {page_title}",
        "url": full_url,
    }


def research_event(event: dict[str, Any]) -> dict[str, Any]:
    query = _research_query(event)
    if not query:
        return {
            "query": "",
            "summary": "",
            "sources": [],
            "error": "沒有可用的 artist 或 event_name",
        }

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    summaries = []
    sources = []
    errors = []

    for source_name, researcher in (
        ("MusicBrainz", _musicbrainz_research),
        ("Wikipedia", _wikipedia_research),
    ):
        try:
            summary, source = researcher(session, query)
            if summary and source:
                summaries.append(summary)
                sources.append(source)
        except (requests.RequestException, ValueError, TypeError, KeyError) as error:
            errors.append(f"{source_name}: {error}")

    result = {
        "query": query,
        "summary": _clean_text(" ".join(summaries))[:MAX_SUMMARY_CHARS],
        "sources": sources,
    }
    if errors:
        result["error"] = "; ".join(errors)
    if not summaries and "error" not in result:
        result["error"] = "找不到可靠的公開音樂背景資料"
    return result
