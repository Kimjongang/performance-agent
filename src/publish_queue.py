import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
JOBS_DIR = PROJECT_ROOT / "bridge" / "jobs"
PUBLISH_STATE_FILE = PROJECT_ROOT / "bridge" / "memory" / "publish_state.json"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
X_DAILY_LIMIT = 2
EMAIL_DAILY_LIMIT = 1
REAL_TICKET_PLATFORMS = {"OPENTIX", "KKTIX", "TICKETPLUS"}


def taipei_today() -> str:
    return datetime.now(TAIPEI_TZ).date().isoformat()


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


def _new_state(date: str) -> dict[str, Any]:
    return {
        "date": date,
        "x_published_count": 0,
        "email_sent_count": 0,
    }


def load_publish_state(
    state_path: Path = PUBLISH_STATE_FILE,
    today: str | None = None,
) -> dict[str, Any]:
    current_date = today or taipei_today()
    if not state_path.exists():
        state = _new_state(current_date)
        _atomic_write_json(state_path, state)
        return state
    try:
        with state_path.open(encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法讀取發布狀態：{error}") from None
    if not isinstance(state, dict):
        raise RuntimeError("發布狀態格式無效")
    if state.get("date") != current_date:
        state = _new_state(current_date)
        _atomic_write_json(state_path, state)
    for key in ("x_published_count", "email_sent_count"):
        value = state.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"發布狀態 {key} 格式無效")
    return state


def _load_jobs(jobs_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    if not jobs_dir.exists():
        return []
    jobs = []
    for path in jobs_dir.glob("*.json"):
        try:
            with path.open(encoding="utf-8") as file:
                job = json.load(file)
            if not isinstance(job, dict):
                raise ValueError("job 不是 JSON object")
            jobs.append((path, job))
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError(f"無法讀取 Bridge job {path.name}：{error}") from None
    return sorted(
        jobs,
        key=lambda item: (
            str(item[1].get("created_at") or ""),
            str(item[1].get("event_id") or ""),
        ),
    )


def _plan_item(job: dict[str, Any], channel: str) -> dict[str, str]:
    content_key = "x_content" if channel == "x" else "email_content"
    return {
        "event_id": str(job.get("event_id") or ""),
        content_key: str(job.get(content_key) or job.get("content") or ""),
    }


def build_publish_plan(
    jobs_dir: Path = JOBS_DIR,
    state_path: Path = PUBLISH_STATE_FILE,
    today: str | None = None,
) -> dict[str, Any]:
    state = load_publish_state(state_path, today=today)
    jobs = _load_jobs(jobs_dir)
    x_remaining = max(0, X_DAILY_LIMIT - state["x_published_count"])
    email_available = state["email_sent_count"] < EMAIL_DAILY_LIMIT

    x_candidates = [
        job
        for _, job in jobs
        if job.get("classification", {}).get("matched") is True
        and job.get("ticket_platform") in REAL_TICKET_PLATFORMS
        and job.get("x_status") == "pending"
        and job.get("actions", {}).get("publish_x") is True
    ]
    email_candidates = [
        job
        for _, job in jobs
        if job.get("classification", {}).get("matched") is True
        and job.get("ticket_platform") in REAL_TICKET_PLATFORMS
        and job.get("email_status") == "pending"
        and job.get("actions", {}).get("send_email") is True
    ]
    return {
        "date": state["date"],
        "x_jobs": [_plan_item(job, "x") for job in x_candidates[:x_remaining]],
        "email_batch": (
            [_plan_item(job, "email") for job in email_candidates]
            if email_available
            else []
        ),
        "limits": {
            "x_daily_limit": X_DAILY_LIMIT,
            "email_daily_limit": EMAIL_DAILY_LIMIT,
        },
        "state": {
            "x_published_count": state["x_published_count"],
            "email_sent_count": state["email_sent_count"],
        },
    }


def _job_file(event_id: str, jobs_dir: Path) -> Path:
    if not event_id or any(char in event_id for char in "\\/:"):
        raise ValueError("event_id 含有不安全的檔名字元")
    path = jobs_dir / f"{event_id}.json"
    if not path.exists():
        raise ValueError(f"找不到 Bridge job：{event_id}")
    return path


def _read_job(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as file:
            job = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"無法讀取 Bridge job：{error}") from None
    if not isinstance(job, dict):
        raise RuntimeError("Bridge job 格式無效")
    return job


def load_publish_job(event_id: str, jobs_dir: Path = JOBS_DIR) -> dict[str, Any]:
    return _read_job(_job_file(event_id, jobs_dir))


def record_x_non_success(
    event_id: str,
    status: str,
    jobs_dir: Path = JOBS_DIR,
) -> None:
    if status not in {"failed", "processing"}:
        raise ValueError("X 非成功狀態只允許 failed 或 processing")
    path = _job_file(event_id, jobs_dir)
    job = _read_job(path)
    if job.get("x_status") != "pending":
        raise RuntimeError(f"job 的 x_status 不是 pending：{event_id}")
    job["x_status"] = status
    job["updated_at"] = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    _atomic_write_json(path, job)


def record_x_success(
    event_id: str,
    jobs_dir: Path = JOBS_DIR,
    state_path: Path = PUBLISH_STATE_FILE,
    today: str | None = None,
) -> None:
    state = load_publish_state(state_path, today=today)
    if state["x_published_count"] >= X_DAILY_LIMIT:
        raise RuntimeError("今日 X 發布額度已滿，不能記錄更多成功發布")
    path = _job_file(event_id, jobs_dir)
    job = _read_job(path)
    if job.get("x_status") != "pending":
        raise RuntimeError(f"job 的 x_status 不是 pending：{event_id}")
    job["x_status"] = "published"
    job["updated_at"] = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    _atomic_write_json(path, job)
    state["x_published_count"] += 1
    _atomic_write_json(state_path, state)


def record_email_success(
    event_ids: list[str],
    jobs_dir: Path = JOBS_DIR,
    state_path: Path = PUBLISH_STATE_FILE,
    today: str | None = None,
) -> None:
    state = load_publish_state(state_path, today=today)
    if state["email_sent_count"] >= EMAIL_DAILY_LIMIT:
        raise RuntimeError("今日 Email 發送額度已滿，不能記錄更多成功發送")
    if not event_ids:
        raise ValueError("Email batch 不可為空")
    loaded = []
    for event_id in event_ids:
        path = _job_file(event_id, jobs_dir)
        job = _read_job(path)
        if job.get("email_status") != "pending":
            raise RuntimeError(f"job 的 email_status 不是 pending：{event_id}")
        loaded.append((path, job))
    timestamp = datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")
    for path, job in loaded:
        job["email_status"] = "sent"
        job["updated_at"] = timestamp
        _atomic_write_json(path, job)
    state["email_sent_count"] += 1
    _atomic_write_json(state_path, state)


def _fake_job(index: int) -> dict[str, Any]:
    return {
        "event_id": f"test_event_{index}",
        "content": f"測試活動 {index}",
        "x_content": f"X 測試活動 {index}",
        "email_content": f"Email 測試活動 {index}",
        "classification": {"matched": True},
        "ticket_platform": "OPENTIX",
        "actions": {"publish_x": True, "send_email": True},
        "x_status": "pending",
        "email_status": "pending",
        "created_at": f"2026-08-11T0{index}:00:00+08:00",
        "updated_at": f"2026-08-11T0{index}:00:00+08:00",
    }


def run_local_queue_tests() -> dict[str, Any]:
    today = "2026-08-11"
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        jobs_dir = root / "bridge" / "jobs"
        state_path = root / "bridge" / "memory" / "publish_state.json"
        jobs_dir.mkdir(parents=True)
        for index in range(1, 6):
            _atomic_write_json(jobs_dir / f"test_event_{index}.json", _fake_job(index))

        for label, x_count, email_count in (
            ("A", 0, 0),
            ("B", 1, 0),
            ("C", 2, 1),
        ):
            _atomic_write_json(
                state_path,
                {
                    "date": today,
                    "x_published_count": x_count,
                    "email_sent_count": email_count,
                },
            )
            plan = build_publish_plan(jobs_dir, state_path, today=today)
            results[label] = {
                "x_jobs": len(plan["x_jobs"]),
                "email_batch": len(plan["email_batch"]),
                "state_unchanged": plan["state"]
                == {
                    "x_published_count": x_count,
                    "email_sent_count": email_count,
                },
            }

        mixed = [_fake_job(index) for index in range(1, 6)]
        mixed[0]["x_status"] = "published"
        mixed[1]["email_status"] = "sent"
        mixed[2]["actions"]["publish_x"] = False
        mixed[3]["actions"]["send_email"] = False
        for job in mixed:
            _atomic_write_json(jobs_dir / f"{job['event_id']}.json", job)
        _atomic_write_json(state_path, _new_state(today))
        plan_d = build_publish_plan(jobs_dir, state_path, today=today)
        results["D"] = {
            "x_event_ids": [item["event_id"] for item in plan_d["x_jobs"]],
            "email_event_ids": [
                item["event_id"] for item in plan_d["email_batch"]
            ],
        }

        for index in range(1, 6):
            _atomic_write_json(jobs_dir / f"test_event_{index}.json", _fake_job(index))
        _atomic_write_json(state_path, _new_state(today))
        record_x_success("test_event_1", jobs_dir, state_path, today=today)
        record_email_success(
            ["test_event_1", "test_event_2"], jobs_dir, state_path, today=today
        )
        record_x_success("test_event_2", jobs_dir, state_path, today=today)
        x_limit_blocked = False
        email_limit_blocked = False
        try:
            record_x_success("test_event_3", jobs_dir, state_path, today=today)
        except RuntimeError:
            x_limit_blocked = True
        try:
            record_email_success(
                ["test_event_3"], jobs_dir, state_path, today=today
            )
        except RuntimeError:
            email_limit_blocked = True
        final_state = load_publish_state(state_path, today=today)
        results["E"] = {
            "x_status": _read_job(jobs_dir / "test_event_1.json")["x_status"],
            "email_statuses": [
                _read_job(jobs_dir / f"test_event_{index}.json")["email_status"]
                for index in (1, 2)
            ],
            "state": final_state,
            "x_limit_blocked": x_limit_blocked,
            "email_limit_blocked": email_limit_blocked,
        }
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return results


if __name__ == "__main__":
    run_local_queue_tests()
