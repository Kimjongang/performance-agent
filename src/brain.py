import json
import os
from typing import Any

import requests


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4.5"

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "needs_research": {"type": "boolean"},
        "genres": {
            "type": "array",
            "items": {"type": "string"},
        },
        "relevance_score": {
            "type": "number",
        },
        "reason": {"type": "string"},
    },
    "required": [
        "matched",
        "needs_research",
        "genres",
        "relevance_score",
        "reason",
    ],
    "additionalProperties": False,
}


def classify_event(
    event_data: dict[str, Any],
    preferences: dict[str, Any],
) -> dict[str, Any]:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY 未設定")

    system_prompt = (
        "你是活動音樂類型分類器。請依音樂語意、藝人背景與活動描述，"
        "判斷活動是否屬於使用者偏好的音樂風格或與其高度相關，"
        "不能只做關鍵字比對。無論使用者選擇何種風格，都應根據音樂語意"
        "理解該風格的子類型、延伸風格、融合風格與高度相關風格。"
        "例如 Jazz 可包含 Bossa Nova、Jazz Fusion 等，Rock 可包含"
        "Indie Rock、Alternative Rock、Post-Rock 等，Classical 可包含"
        "Symphony、Chamber Music、Concerto 等；這些都只是理解方式的例子，"
        "不是固定、封閉或唯一清單，也不可將它們視為硬編碼規則。"
        "判斷必須以使用者實際選擇的 genres 為準。若活動頁資訊不足以可靠判斷，"
        "needs_research 必須為 true。reason 請簡短，"
        "relevance_score 必須介於 0 與 1。只根據提供的資料判斷。"
    )

    user_prompt = json.dumps(
        {
            "preferences": preferences,
            "event": event_data,
        },
        ensure_ascii=False,
    )

    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "event_music_classification",
                "strict": True,
                "schema": RESULT_SCHEMA,
            },
        },
        "provider": {
            "require_parameters": True,
        },
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
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        relevance_score = result.get("relevance_score")
        if (
            isinstance(relevance_score, bool)
            or not isinstance(relevance_score, (int, float))
            or not 0 <= relevance_score <= 1
        ):
            raise RuntimeError("relevance_score 必須是介於 0 與 1 的數值")
        return result
    except requests.HTTPError as error:
        status_code = error.response.status_code
        safe_body = error.response.text.replace(api_key, "[REDACTED]")
        raise RuntimeError(
            "OpenRouter API 呼叫失敗："
            f"HTTP {status_code}\n{safe_body}"
        ) from None
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        safe_message = str(error).replace(api_key, "[REDACTED]")
        raise RuntimeError(
            f"OpenRouter API 呼叫失敗：{safe_message}"
        ) from None
