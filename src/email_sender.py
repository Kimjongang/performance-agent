import json
import os
import re
import smtplib
import ssl
import tempfile
import uuid
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTCOMES_DIR = PROJECT_ROOT / "bridge" / "outcomes"
RECIPIENTS_FILE = PROJECT_ROOT / "config" / "recipients.json"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


def _now() -> datetime:
    return datetime.now(TAIPEI_TZ)


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


def _write_outcome(
    *,
    event_ids: list[str],
    recipients: list[str],
    subject: str,
    attempted_at: str,
    success: bool,
    completed_at: str,
    error: str | None,
    dry_run: bool,
    outcomes_dir: Path,
) -> Path:
    outcome = {
        "channel": "email",
        "success": success,
        "event_ids": event_ids,
        "recipients": recipients,
        "subject": subject,
        "attempted_at": attempted_at,
        "completed_at": completed_at,
        "error": error,
        "dry_run": dry_run,
    }
    timestamp = attempted_at.replace(":", "").replace("-", "")
    filename = f"email_{timestamp}_{uuid.uuid4().hex[:8]}.json"
    path = outcomes_dir / filename
    _atomic_write_json(path, outcome)
    return path


def _validate_batch(email_batch: list[dict[str, Any]]) -> tuple[list[str], str]:
    if not isinstance(email_batch, list) or not email_batch:
        raise ValueError("email_batch 必須是非空 list")
    event_ids = []
    contents = []
    for item in email_batch:
        if not isinstance(item, dict):
            raise ValueError("email_batch 每筆資料必須是 object")
        event_id = str(item.get("event_id") or "").strip()
        content = str(item.get("email_content") or item.get("content") or "").strip()
        if not event_id or not content:
            raise ValueError("email_batch 每筆都必須包含 event_id 與 content")
        event_ids.append(event_id)
        contents.append(content)
    return event_ids, "\n\n--------------------\n\n".join(contents)


def load_email_recipients(
    recipients_path: Path = RECIPIENTS_FILE,
) -> list[str]:
    if not recipients_path.exists():
        raise RuntimeError(f"Email 收件人設定檔不存在：{recipients_path}")
    try:
        with recipients_path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法讀取 Email 收件人設定：{error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("email_recipients"), list):
        raise RuntimeError("recipients.json 的 email_recipients 必須是 list")
    recipients = []
    seen = set()
    for value in data["email_recipients"]:
        if not isinstance(value, str):
            continue
        email = value.strip()
        normalized = email.casefold()
        if (
            not email
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        recipients.append(email)
    if not recipients:
        raise RuntimeError("recipients.json 沒有任何有效 Email 收件人")
    return recipients


def send_email_batch(
    email_batch: list[dict[str, Any]],
    dry_run: bool = False,
    *,
    test_mode: bool = False,
    outcomes_dir: Path = OUTCOMES_DIR,
    recipients_path: Path = RECIPIENTS_FILE,
) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env")
    sender = os.getenv("EMAIL_ADDRESS", "").strip()
    password = os.getenv("EMAIL_APP_PASSWORD", "").strip()
    if not sender or not password:
        raise RuntimeError("EMAIL_ADDRESS、EMAIL_APP_PASSWORD 必須完整設定")
    recipients = load_email_recipients(recipients_path)

    event_ids, body = _validate_batch(email_batch)
    attempted = _now()
    prefix = "【測試】" if test_mode else ""
    subject = f"{prefix}演出活動通知｜{attempted.date().isoformat()}"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message.set_content(body)
    attempted_at = attempted.isoformat(timespec="seconds")

    if dry_run:
        completed_at = _now().isoformat(timespec="seconds")
        outcome_path = _write_outcome(
            event_ids=event_ids,
            recipients=recipients,
            subject=subject,
            attempted_at=attempted_at,
            success=True,
            completed_at=completed_at,
            error=None,
            dry_run=True,
            outcomes_dir=outcomes_dir,
        )
        return {
            "success": True,
            "event_ids": event_ids,
            "recipients": recipients,
            "subject": subject,
            "sent_at": completed_at,
            "body": body,
            "dry_run": True,
            "outcome_path": str(outcome_path),
        }

    try:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            context=ssl.create_default_context(),
            timeout=30,
        ) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        completed_at = _now().isoformat(timespec="seconds")
        safe_error = str(error).replace(password, "[REDACTED]")
        _write_outcome(
            event_ids=event_ids,
            recipients=recipients,
            subject=subject,
            attempted_at=attempted_at,
            success=False,
            completed_at=completed_at,
            error=safe_error,
            dry_run=False,
            outcomes_dir=outcomes_dir,
        )
        raise RuntimeError(f"Email 寄送失敗：{safe_error}") from None

    completed_at = _now().isoformat(timespec="seconds")
    outcome_path = _write_outcome(
        event_ids=event_ids,
        recipients=recipients,
        subject=subject,
        attempted_at=attempted_at,
        success=True,
        completed_at=completed_at,
        error=None,
        dry_run=False,
        outcomes_dir=outcomes_dir,
    )
    return {
        "success": True,
        "event_ids": event_ids,
        "recipients": recipients,
        "subject": subject,
        "sent_at": completed_at,
        "outcome_path": str(outcome_path),
    }


def _masked_email(value: str) -> str:
    local, separator, domain = value.partition("@")
    if not separator:
        return "***"
    visible = local[:2] if len(local) > 2 else local[:1]
    return f"{visible}***@{domain}"


def successful_email_event_ids(
    outcomes_dir: Path = OUTCOMES_DIR,
) -> set[str]:
    successful: set[str] = set()
    if not outcomes_dir.exists():
        return successful
    for path in outcomes_dir.glob("email_*.json"):
        try:
            with path.open(encoding="utf-8") as file:
                outcome = json.load(file)
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(outcome, dict)
            and outcome.get("channel") == "email"
            and outcome.get("success") is True
            and outcome.get("dry_run") is False
        ):
            successful.update(
                str(event_id)
                for event_id in outcome.get("event_ids", [])
                if event_id
            )
    return successful


def run_dry_run_test() -> dict[str, Any]:
    dry_batch = [
        {"event_id": "test_email_1", "content": "測試活動一：Jazz 現場演出。"},
        {"event_id": "test_email_2", "content": "測試活動二：Bossa Nova 音樂會。"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        test_root = Path(directory)
        recipients_path = test_root / "recipients.json"
        recipients_path.write_text(
            json.dumps(
                {
                    "email_recipients": [
                        " first@example.com ",
                        "second@example.com",
                        "first@example.com",
                        "",
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        dry_result = send_email_batch(
            dry_batch,
            dry_run=True,
            outcomes_dir=test_root / "outcomes",
            recipients_path=recipients_path,
        )
    safe_dry_result = {
        **dry_result,
        "recipients": [_masked_email(value) for value in dry_result["recipients"]],
    }
    print(json.dumps({"dry_run": safe_dry_result}, ensure_ascii=False, indent=2))
    return safe_dry_result


def run_smtp_test() -> dict[str, Any]:
    smtp_batch = [
        {
            "event_id": "test_email_smtp",
            "content": "這是一封 concert-agent Email Hands 的 SMTP 測試信。",
        }
    ]
    smtp_result = send_email_batch(smtp_batch, test_mode=True)
    safe_smtp_result = {
        **smtp_result,
        "recipients": [_masked_email(value) for value in smtp_result["recipients"]],
    }
    print(json.dumps({"smtp_test": safe_smtp_result}, ensure_ascii=False, indent=2))
    return safe_smtp_result


if __name__ == "__main__":
    run_dry_run_test()
