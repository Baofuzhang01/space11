from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import socket
import sys
from typing import Any

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server_store.report_reserve_results import (  # noqa: E402
    BEIJING_TZ,
    build_result,
    extract_log_timestamp,
    normalize_text,
    post_json,
)


def load_json_text(text: str, default: Any) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return default


def pick_user_payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}

    account = normalize_text(os.getenv("CX_USERNAME"), 120)
    users = payload.get("users")
    if not isinstance(users, list):
        users = payload.get("reserve")
    if not isinstance(users, list):
        return payload

    selected = {}
    for user in users:
        if not isinstance(user, dict):
            continue
        user_account = normalize_text(user.get("phone") or user.get("username"), 120)
        if account and user_account == account:
            selected = user
            break
        if not selected:
            selected = user
    if not selected:
        return {}

    inherited = {key: value for key, value in payload.items() if key not in {"users", "reserve"}}
    return {**inherited, **selected}


def load_payload(args_payload: str) -> dict:
    raw_payload = args_payload or os.getenv("DISPATCH_PAYLOAD") or ""
    payload = load_json_text(raw_payload, {}) if raw_payload else {}
    if isinstance(payload, dict):
        selected = pick_user_payload(payload)
        if selected:
            return selected

    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path:
        try:
            event = json.loads(pathlib.Path(event_path).read_text(encoding="utf-8"))
        except Exception:
            event = {}
        client_payload = event.get("client_payload") if isinstance(event, dict) else {}
        if isinstance(client_payload, dict):
            return pick_user_payload(client_payload)
    return {}


def first_log_time(log_path: pathlib.Path) -> str:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return ""
    for line in lines:
        timestamp = extract_log_timestamp(line)
        if timestamp:
            return timestamp
    return ""


def finished_time(log_path: pathlib.Path) -> str:
    try:
        mtime = dt.datetime.fromtimestamp(log_path.stat().st_mtime, BEIJING_TZ)
    except Exception:
        return dt.datetime.now(BEIJING_TZ)
    return mtime.isoformat(timespec="seconds")


def github_run_id() -> str:
    parts = [
        "github",
        normalize_text(os.getenv("GITHUB_REPOSITORY"), 120).replace("/", "_"),
        normalize_text(os.getenv("GITHUB_RUN_ID"), 80),
        normalize_text(os.getenv("GITHUB_RUN_ATTEMPT"), 20),
        normalize_text(os.getenv("GITHUB_JOB"), 80),
    ]
    return "_".join(part for part in parts if part)


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse GitHub Actions reserve.log and report result center record")
    parser.add_argument("--log", default="reserve.log")
    parser.add_argument("--payload-json", default="")
    parser.add_argument("--center-url", default=os.getenv("RESERVE_RESULT_CENTER_URL") or "")
    parser.add_argument("--token", default=os.getenv("RESERVE_RESULT_REPORT_TOKEN") or "")
    parser.add_argument("--server-id", default=os.getenv("RESERVE_RESULT_SERVER_ID") or github_run_id() or socket.gethostname())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    log_path = pathlib.Path(args.log).expanduser().resolve()
    if not log_path.exists():
        print(json.dumps({"ok": True, "skipped": True, "reason": f"log not found: {log_path}"}, ensure_ascii=False))
        return 0

    user = load_payload(args.payload_json)
    if not isinstance(user, dict):
        user = {}

    account = normalize_text(user.get("phone") or user.get("username") or os.getenv("CX_USERNAME"), 120)
    run_id = github_run_id() or f"github_{dt.datetime.now(BEIJING_TZ).strftime('%Y%m%d_%H%M%S')}"
    summary = {
        "run_id": run_id,
        "started_at": first_log_time(log_path),
        "finished_at": finished_time(log_path),
    }
    payload = {"users": [user], **user}
    item = {
        "index": 1,
        "username": account,
        "log_path": str(log_path),
        "returncode": int(os.getenv("RESERVE_ACTION_RETURN_CODE") or "0"),
        "started_at": summary["started_at"],
        "finished_at": summary["finished_at"],
    }
    result = build_result(PROJECT_ROOT, summary, payload, item, normalize_text(args.server_id, 120))
    result["task_id"] = "_".join(
        part
        for part in [
            run_id,
            normalize_text(result.get("school_id"), 80),
            normalize_text(result.get("user_id") or account, 120),
        ]
        if part
    )
    result.setdefault("raw", {})
    result["raw"]["github"] = {
        "repository": os.getenv("GITHUB_REPOSITORY") or "",
        "runId": os.getenv("GITHUB_RUN_ID") or "",
        "runAttempt": os.getenv("GITHUB_RUN_ATTEMPT") or "",
        "job": os.getenv("GITHUB_JOB") or "",
        "workflow": os.getenv("GITHUB_WORKFLOW") or "",
        "runUrl": f"https://github.com/{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}",
    }

    if args.dry_run:
        print(json.dumps({"ok": True, "dryRun": True, "result": result}, ensure_ascii=False, indent=2))
        return 0

    if not args.token:
        print(json.dumps({"ok": True, "skipped": True, "reason": "RESERVE_RESULT_REPORT_TOKEN not configured"}, ensure_ascii=False))
        return 0
    if not str(args.center_url or "").strip():
        print(json.dumps({"ok": True, "skipped": True, "reason": "RESERVE_RESULT_CENTER_URL not configured"}, ensure_ascii=False))
        return 0

    response = post_json(f"{args.center_url.rstrip('/')}/api/reserve-results/report", args.token, result, 10)
    print(json.dumps({"ok": bool(response.get("ok")), "response": response, "taskId": result.get("task_id")}, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
