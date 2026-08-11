import json
import os
import re
from typing import Any

import requests

from brain import MODEL, OPENROUTER_URL


CONTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "x_content": {"type": "string"},
        "email_content": {"type": "string"},
    },
    "required": ["x_content", "email_content"],
    "additionalProperties": False,
}


def x_weighted_length(content: str) -> int:
    urls = list(re.finditer(r"https?://\S+", content))
    total = 0
    position = 0
    for match in urls:
        total += sum(1 if ord(char) < 128 else 2 for char in content[position:match.start()])
        total += 23
        position = match.end()
    total += sum(1 if ord(char) < 128 else 2 for char in content[position:])
    return total


def _short_datetime(value: Any) -> str:
    text = str(value or "")
    match = re.match(r"\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})", text)
    if not match:
        return text
    month, day, hour, minute = match.groups()
    return f"{int(month)}/{int(day)} {hour}:{minute}"


def _truncate_weighted(text: str, maximum: int) -> str:
    if x_weighted_length(text) <= maximum:
        return text
    result = ""
    for char in text:
        candidate = result + char + "…"
        if x_weighted_length(candidate) > maximum:
            break
        result += char
    return result.rstrip() + "…"


def build_x_fallback(
    event: dict[str, Any], classification: dict[str, Any]
) -> str:
    url = str(event.get("url") or "").strip()
    name = str(event.get("event_name") or "").strip()
    dates = event.get("performance_dates") or []
    date = _short_datetime(dates[0]) if isinstance(dates, list) and dates else ""
    venue = str(event.get("venue") or "").strip()
    city = str(event.get("city") or "").strip()
    location = "・".join(dict.fromkeys(item for item in (city, venue) if item))
    artist = str(event.get("artist") or "").strip()
    genres = "、".join(str(item) for item in classification.get("genres", [])[:2])
    sale_time = _short_datetime(event.get("sale_time"))
    platform = str(event.get("ticket_platform") or "").strip()

    optional = [
        f"{date}{'・另有其他場次' if len(dates) > 1 else ''}" if date else "",
        location,
        artist if artist and artist.casefold() not in name.casefold() else "",
        f"喜歡{genres}可以留意。" if genres else "",
        f"{sale_time}開賣・{platform}" if sale_time else platform,
    ]
    kept = [item for item in optional if item]

    def compose(title: str, extras: list[str]) -> str:
        middle = "\n".join([title, *extras])
        return f"🎵 {middle}\n{url}" if url else f"🎵 {middle}"

    content = compose(name, kept)
    while kept and x_weighted_length(content) > 280:
        kept.pop()
        content = compose(name, kept)
    if x_weighted_length(content) > 280:
        reserved = x_weighted_length(compose("", []))
        name = _truncate_weighted(name, max(1, 280 - reserved))
        content = compose(name, [])
    if x_weighted_length(content) > 280:
        raise RuntimeError("活動名稱與網址本身已超過 X 字數限制")
    return content


def generate_content(
    event: dict[str, Any],
    classification: dict[str, Any],
    preferences: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未設定")

    dates = event.get("performance_dates", [])
    shown_dates = dates[:3] if isinstance(dates, list) else []
    content_input = {
        "event_name": event.get("event_name"),
        "artist": event.get("artist"),
        "description": event.get("description"),
        "performance_dates": shown_dates,
        "has_more_performance_dates": isinstance(dates, list) and len(dates) > 3,
        "city": event.get("city"),
        "venue": event.get("venue"),
        "sale_time": event.get("sale_time"),
        "ticket_platform": event.get("ticket_platform"),
        "url": event.get("url"),
        "classification_genres": classification.get("genres", []),
        "classification_reason": classification.get("reason", ""),
        "preferences": preferences.get("genres", []),
        "research": event.get("research"),
    }
    system_prompt = (
        "你是音樂藝文社群編輯。只根據輸入事實，一次撰寫繁體中文 x_content 與"
        "email_content。兩者都要自然、有資訊價值，不使用欄位式排列，不提 AI、Claude、"
        "matched、偏好或 relevance_score，也不得猜測票價、售罄、加場、首度來台等事實。"
        "x_content 是單則 X 貼文：完整保留 URL，依 X 加權字數不超過 280，建議 220 以內；"
        "優先保留活動名稱、已有藝人、一句音樂特色、最早場次短日期、場館或城市與 URL，"
        "多場只寫最早場並可註明另有場次，售票時間與平台有餘裕才寫，可少量使用 emoji。"
        "email_content 不受 280 限制，可較詳細但仍簡潔好讀；自然包含已有藝人、音樂特色、"
        "可用演出日期（大量場次不要全列）、城市場館、明確開賣時間、售票平台與 URL。"
        "artist 或 sale_time 空白時省略，不要顯示未知欄位。"
    )
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(content_input, ensure_ascii=False),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "shared_event_content",
                "strict": True,
                "schema": CONTENT_SCHEMA,
            },
        },
        "provider": {"require_parameters": True},
        "temperature": 0,
    }
    try:
        response = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        result = json.loads(response.json()["choices"][0]["message"]["content"])
        x_content = result.get("x_content")
        email_content = result.get("email_content")
        if not isinstance(x_content, str) or not x_content.strip():
            raise RuntimeError("文案 x_content 不可為空")
        if not isinstance(email_content, str) or not email_content.strip():
            raise RuntimeError("文案 email_content 不可為空")
        x_content = x_content.strip()
        fallback_used = x_weighted_length(x_content) > 280
        if fallback_used:
            x_content = build_x_fallback(event, classification)
        return {
            "x_content": x_content,
            "email_content": email_content.strip(),
            "x_fallback_used": fallback_used,
            "x_weighted_length": x_weighted_length(x_content),
        }
    except requests.HTTPError as error:
        body = error.response.text.replace(api_key, "[REDACTED]")
        raise RuntimeError(
            f"OpenRouter 文案產生失敗：HTTP {error.response.status_code}\n{body}"
        ) from None
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        message = str(error).replace(api_key, "[REDACTED]")
        raise RuntimeError(f"OpenRouter 文案產生失敗：{message}") from None
