import argparse
import concurrent.futures
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys


DEFAULT_SERVER_PROJECT_ROOT = "/opt/Main_ChaoXingReserveSeat"
ROOT_DIR = pathlib.Path(
    os.getenv("SERVER_PROJECT_ROOT", DEFAULT_SERVER_PROJECT_ROOT)
).resolve()
RUNS_DIR = ROOT_DIR / "server_runs"


def _beijing_now() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def _safe_name(raw: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", str(raw or "").strip())
    return text[:60] or "user"


def _load_payload(payload_file: str) -> dict:
    with open(payload_file, "r", encoding="utf-8") as f:
        return json.load(f)


def _iter_users(payload: dict) -> list[dict]:
    users = payload.get("users")
    if isinstance(users, list) and users:
        return users
    return [payload]


def _build_user_dispatch_payload(payload: dict, user: dict) -> dict:
    merged = dict(user)
    inherited_keys = [
        "strategy",
        "endtime",
        "seat_api_mode",
        "reserve_next_day",
        "enable_slider",
        "enable_textclick",
    ]
    for key in inherited_keys:
        if key not in merged and key in payload:
            merged[key] = payload.get(key)
    return merged


def _run_one(user: dict, index: int, run_dir: pathlib.Path, payload: dict) -> dict:
    username = str(user.get("username", "")).strip()
    remark = (
        user.get("remark")
        or user.get("comments")
        or ""
    )
    nickname = (
        user.get("nickname")
        or user.get("nickName")
        or user.get("name")
        or remark
        or username
        or f"user_{index + 1}"
    )
    log_name = remark or nickname or username
    log_path = run_dir / f"{index + 1:02d}_{_safe_name(log_name)}.log"
    env = os.environ.copy()
    dispatch_payload = _build_user_dispatch_payload(payload, user)
    env["DISPATCH_PAYLOAD"] = json.dumps(dispatch_payload, ensure_ascii=False)

    cmd = [sys.executable, "main.py", "--action", "--dispatch"]
    started_at = _beijing_now().isoformat()
    with open(log_path, "w", encoding="utf-8") as log_file:
        log_file.write(f"[batch] started_at={started_at}\n")
        log_file.write(f"[batch] user={nickname}\n")
        log_file.write(f"[batch] cmd={' '.join(cmd)}\n\n")
        log_file.flush()
        proc = subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )

    return {
        "index": index + 1,
        "username": username,
        "display_name": remark or nickname or username,
        "remark": remark,
        "nickname": nickname,
        "returncode": proc.returncode,
        "log_path": str(log_path),
        "started_at": started_at,
        "finished_at": _beijing_now().isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description="Run dispatch payload users in local batch mode")
    parser.add_argument("--payload-file", required=True, help="Path to dispatch payload JSON")
    parser.add_argument("--concurrency", type=int, default=0, help="Override max concurrency")
    args = parser.parse_args()

    payload = _load_payload(args.payload_file)
    users = _iter_users(payload)
    max_concurrency = args.concurrency or int(payload.get("server_max_concurrency") or 13)
    max_concurrency = max(1, min(max_concurrency, len(users)))

    run_id = payload.get("run_id") or _beijing_now().strftime("%Y%m%d_%H%M%S_%f")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "run_id": run_id,
        "school_id": payload.get("school_id", ""),
        "school_name": payload.get("school_name", ""),
        "batch_index": payload.get("batch_index"),
        "batch_total": payload.get("batch_total"),
        "user_count": len(users),
        "max_concurrency": max_concurrency,
        "started_at": _beijing_now().isoformat(),
        "results": [],
    }

    with open(run_dir / "payload.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(_run_one, user, idx, run_dir, payload)
            for idx, user in enumerate(users)
        ]
        for future in concurrent.futures.as_completed(futures):
            summary["results"].append(future.result())

    summary["results"].sort(key=lambda item: item["index"])
    summary["finished_at"] = _beijing_now().isoformat()
    summary["failed"] = sum(1 for item in summary["results"] if item["returncode"] != 0)

    with open(run_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(1 if summary["failed"] else 0)


if __name__ == "__main__":
    main()
