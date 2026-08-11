import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from content_generator import x_weighted_length


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTCOMES_DIR = PROJECT_ROOT / "bridge" / "outcomes"
API_BASE = "https://backend.blotato.com/v2"
TARGET_USERNAME = "AITEAM1gm"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
REQUEST_TIMEOUT = 30
POLL_ATTEMPTS = 6
POLL_INTERVAL = 5
REAL_PLATFORMS = {
    "OPENTIX": ("opentix_", "www.opentix.life"),
    "KKTIX": ("kktix_", None),
    "TICKETPLUS": ("ticketplus_", "ticketplus.com.tw"),
}
FORBIDDEN_TEXT = (
    "【測試】",
    "concert-agent test",
    "concert-agent 測試",
    "example.test",
    "假活動",
    "lisa ono 測試",
)


def _now() -> str:
    return datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_outcome(outcome: dict[str, Any]) -> Path:
    timestamp = outcome["attempted_at"].replace(":", "").replace("-", "")
    path = OUTCOMES_DIR / f"x_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    _atomic_write_json(path, outcome)
    return path


def _headers(api_key: str) -> dict[str, str]:
    return {"blotato-api-key": api_key, "Content-Type": "application/json"}


def resolve_x_account(session: requests.Session, api_key: str) -> str:
    configured = os.getenv("BLOTATO_X_ACCOUNT_ID", "").strip()
    response = session.get(
        f"{API_BASE}/users/me/accounts",
        params={"platform": "twitter"},
        headers=_headers(api_key),
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    items = response.json().get("items", [])
    matches = [
        item
        for item in items
        if str(item.get("platform") or "") == "twitter"
        and str(item.get("username") or "").lstrip("@").casefold()
        == TARGET_USERNAME.casefold()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"無法唯一確認 Blotato X 帳號 @{TARGET_USERNAME}，已停止發布"
        )
    confirmed_id = str(matches[0].get("id") or "").strip()
    if not confirmed_id:
        raise RuntimeError("Blotato Accounts API 未回傳有效 account ID")
    if configured and configured != confirmed_id:
        raise RuntimeError(
            "BLOTATO_X_ACCOUNT_ID 與 Accounts API 的目標帳號不一致，已停止發布"
        )
    return confirmed_id


def _validate_real_job(job: dict[str, Any]) -> str:
    event_id = str(job.get("event_id") or "").strip()
    platform = str(job.get("ticket_platform") or "").strip()
    url = str(job.get("url") or "").strip()
    content = str(job.get("x_content") or job.get("content") or "").strip()
    if job.get("classification", {}).get("matched") is not True:
        raise ValueError("只允許發布 final classification matched=true 的 job")
    if platform not in REAL_PLATFORMS:
        raise ValueError("拒絕發布非正式售票平台 job")
    prefix, expected_host = REAL_PLATFORMS[platform]
    if not event_id.startswith(prefix):
        raise ValueError("event_id 與售票平台不一致")
    host = urlsplit(url).hostname or ""
    if platform == "KKTIX":
        if not host.endswith(".kktix.cc"):
            raise ValueError("KKTIX job URL 不是正式活動網址")
    elif host != expected_host:
        raise ValueError("job URL 不是正式售票平台網址")
    lowered = content.casefold()
    if any(term.casefold() in lowered for term in FORBIDDEN_TEXT):
        raise ValueError("文案含禁止發布的測試內容")
    if not content or url not in content:
        raise ValueError("文案必須包含完整正式活動網址")
    if x_weighted_length(content) > 280:
        raise ValueError("文案超過 X 280 加權字數限制，不可直接發布")
    return content


def build_x_payload(job: dict[str, Any], account_id: str) -> dict[str, Any]:
    content = _validate_real_job(job)
    return {
        "post": {
            "accountId": account_id,
            "content": {
                "text": content,
                "mediaUrls": [],
                "platform": "twitter",
            },
            "target": {"targetType": "twitter"},
        }
    }


def publish_x(job: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("BLOTATO_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("BLOTATO_API_KEY 未設定")
    attempted_at = _now()
    event_id = str(job.get("event_id") or "").strip()
    submission_id = ""
    if dry_run:
        payload = build_x_payload(job, "DRY_RUN_ACCOUNT")
        return {"success": True, "event_id": event_id, "payload": payload}

    session = requests.Session()
    try:
        account_id = resolve_x_account(session, api_key)
        payload = build_x_payload(job, account_id)
        response = session.post(
            f"{API_BASE}/posts",
            headers=_headers(api_key),
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        submission_id = str(response.json().get("postSubmissionId") or "").strip()
        if not submission_id:
            raise RuntimeError("Blotato 未回傳 postSubmissionId")

        status_data: dict[str, Any] = {}
        for attempt in range(POLL_ATTEMPTS):
            if attempt:
                time.sleep(POLL_INTERVAL)
            status_response = session.get(
                f"{API_BASE}/posts/{submission_id}",
                headers=_headers(api_key),
                timeout=REQUEST_TIMEOUT,
            )
            status_response.raise_for_status()
            status_data = status_response.json()
            status = status_data.get("status")
            if status in {"published", "failed"}:
                break
        status = str(status_data.get("status") or "")
        public_url = str(status_data.get("publicUrl") or "").strip()
        if status != "published":
            message = str(status_data.get("errorMessage") or status or "狀態確認逾時")
            completed_at = _now()
            outcome = {
                "channel": "x",
                "success": False,
                "event_ids": [event_id],
                "post_id": "",
                "post_url": "",
                "submission_id": submission_id,
                "attempted_at": attempted_at,
                "completed_at": completed_at,
                "error": f"Blotato 發布未成功：{message}",
            }
            outcome_path = _write_outcome(outcome)
            queue_status = "failed" if status == "failed" else "processing"
            return {
                **outcome,
                "queue_status": queue_status,
                "outcome_path": str(outcome_path),
            }
        if not public_url:
            raise RuntimeError("Blotato 已發布但未回傳 publicUrl")
        post_id_match = re.search(r"/status/(\d+)", public_url)
        post_id = post_id_match.group(1) if post_id_match else ""
        completed_at = _now()
        outcome = {
            "channel": "x",
            "success": True,
            "event_ids": [event_id],
            "post_id": post_id,
            "post_url": public_url,
            "submission_id": submission_id,
            "attempted_at": attempted_at,
            "completed_at": completed_at,
            "error": None,
        }
        outcome_path = _write_outcome(outcome)
        return {**outcome, "outcome_path": str(outcome_path)}
    except Exception as error:
        safe_error = str(error).replace(api_key, "[REDACTED]")
        outcome = {
            "channel": "x",
            "success": False,
            "event_ids": [event_id],
            "post_id": "",
            "post_url": "",
            "submission_id": submission_id,
            "attempted_at": attempted_at,
            "completed_at": _now(),
            "error": safe_error,
        }
        outcome_path = _write_outcome(outcome)
        raise RuntimeError(
            f"X 發布失敗：{safe_error}；outcome={outcome_path.name}"
        ) from None
