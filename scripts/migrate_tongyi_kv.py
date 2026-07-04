#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXPORT_PATH = PROJECT_ROOT / "tongyi-kv-export.json"
DEFAULT_ENV_EXPORT_PATH = PROJECT_ROOT / "tongyi-env-migration.env"

SERVER_ENV_KEYS = [
    "CF_ACCOUNT_ID",
    "CF_KV_NAMESPACE_ID",
    "CF_API_TOKEN",
    "FLASK_SECRET_KEY",
    "SERVER_PROJECT_ROOT",
    "SEAT_STORE_DB_PATH",
    "SERVER_DISPATCH_HOST",
    "SERVER_DISPATCH_PORT",
    "SERVER_DISPATCH_API_KEY",
    "ENABLE_RESERVE_RESULT_CENTER",
    "RESERVE_RESULT_REPORT_TOKEN",
    "RESERVE_RESULT_CENTER_URL",
    "RESERVE_RESULT_SERVER_ID",
    "RESERVE_RESULT_REPORT_TIMEOUT",
    "ENABLE_RESERVE_RESULT_REPORT_TIMER",
    "RESERVE_RESULT_LOCAL_WRITE",
    "SERVER_WORKER2_ENABLED",
    "SERVER_WORKER2_TRIGGER_API",
    "SERVER_WORKER2_API_KEY",
    "SERVER_WORKER2_UI_KEY",
    "SERVER_WORKER2_HEARTBEAT_SOURCE_ACCOUNT_ID",
    "SERVER_WORKER2_HEARTBEAT_SOURCE_NAMESPACE_ID",
    "SERVER_WORKER2_HEARTBEAT_SOURCE_API_TOKEN",
    "SERVER_WORKER2_FEISHU_WEBHOOK",
    "SERVER_WORKER2_FEISHU_KEYWORD",
    "SERVER_WORKER2_RECORDS_DIR",
    "SERVER_WORKER2_SCHEDULE_FILE",
    "SERVER_WORKER2_ALLOW_TIMER_EDIT",
    "SERVER_WORKER2_TIMER_UNIT_PATH",
    "SERVER_WORKER2_TIMER_NAME",
    "SERVER_WORKER2_SERVICE_NAME",
    "SERVER_WORKER2_UI_HOST",
    "SERVER_WORKER2_UI_PORT",
    "CHAOJIYING_USERNAME",
    "CHAOJIYING_PASSWORD",
    "CHAOJIYING_SOFT_ID",
    "CHAOJIYING_CODETYPE",
    "TULINGCLOUD_USERNAME",
    "TULINGCLOUD_PASSWORD",
]

WORKER_SECRET_NAMES = [
    "API_KEY",
    "GH_TOKEN",
    "GH_TOKEN_A",
    "GH_TOKEN_B",
    "GH_TOKEN_C",
    "GH_TOKEN_D",
    "GH_TOKEN_E",
    "SERVER_DISPATCH_API_KEY",
]


def load_env_file(path: str | None) -> None:
    if not path:
        return
    env_path = Path(path).expanduser()
    if not env_path.exists():
        raise SystemExit(f"env file not found: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
                os.environ[key] = value


def parse_env_file(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    env_path = Path(path).expanduser()
    if not env_path.exists():
        raise SystemExit(f"env file not found: {env_path}")
    result: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip()
    return result


def env_or_arg(args, attr: str, env_names: str | list[str], label: str) -> str:
    names = [env_names] if isinstance(env_names, str) else env_names
    value = str(getattr(args, attr, "") or "").strip()
    if not value:
        for env_name in names:
            value = str(os.getenv(env_name, "")).strip()
            if value:
                break
    if not value:
        raise SystemExit(
            f"missing {label}: pass --{attr.replace('_', '-')} or set one of {', '.join(names)}"
        )
    return value


class CloudflareKV:
    def __init__(self, account_id: str, namespace_id: str, api_token: str):
        self.account_id = account_id
        self.namespace_id = namespace_id
        self.base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
            f"/storage/kv/namespaces/{namespace_id}"
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        response = self.session.request(method, url, timeout=kwargs.pop("timeout", 30), **kwargs)
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise RuntimeError(f"Cloudflare API {method} {url} failed: {response.status_code} {detail}")
        return response

    def list_keys(self, prefix: str = "") -> list[dict[str, Any]]:
        keys: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params = {"limit": 1000}
            if prefix:
                params["prefix"] = prefix
            if cursor:
                params["cursor"] = cursor
            response = self._request("GET", f"{self.base_url}/keys", params=params)
            payload = response.json()
            if not payload.get("success", False):
                raise RuntimeError(f"Cloudflare list keys failed: {payload}")
            keys.extend(payload.get("result") or [])
            info = payload.get("result_info") or {}
            cursor = str(info.get("cursor") or "")
            if not cursor:
                return keys

    def get_value_bytes(self, key: str) -> bytes | None:
        response = self.session.get(f"{self.base_url}/values/{key}", timeout=30)
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RuntimeError(
                f"Cloudflare get value failed for {key!r}: {response.status_code} {response.text[:1000]}"
            )
        return response.content

    def put_value_bytes(
        self,
        key: str,
        value: bytes,
        metadata: Any = None,
        expiration: int | None = None,
    ) -> None:
        files = None
        headers = None
        params = {"expiration": expiration} if expiration is not None else None
        if metadata is not None:
            files = {
                "value": (None, value),
                "metadata": (None, json.dumps(metadata, ensure_ascii=False)),
            }
        else:
            headers = {"Content-Type": "text/plain; charset=utf-8"}
        response = self.session.put(
            f"{self.base_url}/values/{key}",
            data=value if files is None else None,
            files=files,
            headers=headers,
            params=params,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Cloudflare put value failed for {key!r}: {response.status_code} {response.text[:1000]}"
            )


def build_source_client(args) -> CloudflareKV:
    profiles = {
        "source": (
            "SOURCE_CF_ACCOUNT_ID",
            "SOURCE_CF_KV_NAMESPACE_ID",
            "SOURCE_CF_API_TOKEN",
        ),
        "current": ("CF_ACCOUNT_ID", "CF_KV_NAMESPACE_ID", "CF_API_TOKEN"),
        "new": (
            "NEW_CF_ACCOUNT_ID",
            "NEW_CF_KV_NAMESPACE_ID",
            "NEW_CF_API_TOKEN",
        ),
    }
    account_env, namespace_env, token_env = profiles[args.source_profile]
    return CloudflareKV(
        env_or_arg(args, "source_account_id", account_env, "source account id"),
        env_or_arg(
            args,
            "source_namespace_id",
            namespace_env,
            "source KV namespace id",
        ),
        env_or_arg(args, "source_api_token", token_env, "source API token"),
    )


def build_target_client(args) -> CloudflareKV:
    return CloudflareKV(
        env_or_arg(args, "target_account_id", "TARGET_CF_ACCOUNT_ID", "target account id"),
        env_or_arg(args, "target_namespace_id", "TARGET_CF_KV_NAMESPACE_ID", "target KV namespace id"),
        env_or_arg(args, "target_api_token", "TARGET_CF_API_TOKEN", "target API token"),
    )


def selected_keys(
    keys: list[dict[str, Any]],
    include_prefixes: list[str],
    exclude_prefixes: list[str],
) -> list[dict[str, Any]]:
    result = []
    for item in keys:
        name = str(item.get("name") or "")
        if include_prefixes and not any(name.startswith(prefix) for prefix in include_prefixes):
            continue
        if exclude_prefixes and any(name.startswith(prefix) for prefix in exclude_prefixes):
            continue
        result.append(item)
    return result


def stable_key_list(
    source: CloudflareKV,
    include_prefixes: list[str],
    exclude_prefixes: list[str],
    attempts: int = 6,
) -> list[dict[str, Any]]:
    previous: list[dict[str, Any]] | None = None
    previous_names: list[str] | None = None
    for _attempt in range(attempts):
        current = selected_keys(source.list_keys(), include_prefixes, exclude_prefixes)
        current.sort(key=lambda item: str(item.get("name") or ""))
        current_names = [str(item.get("name") or "") for item in current]
        if current_names == previous_names:
            return current
        previous = current
        previous_names = current_names
        time.sleep(1)
    raise SystemExit(
        f"source KV key listing did not stabilize after {attempts} attempts; "
        f"last count={len(previous_names or [])}"
    )


def record_digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def manifest_digest(records: list[dict[str, Any]]) -> str:
    manifest = [
        {
            "key": record["key"],
            "sha256": record["sha256"],
            "metadata": record.get("metadata"),
            "expiration": record.get("expiration"),
        }
        for record in records
    ]
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def load_partial_export(
    path: Path,
    source: CloudflareKV,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    source_info = payload.get("source") or {}
    if (
        payload.get("format") != "tongyi-cloudflare-kv-export-partial-v1"
        or source_info.get("account_id") != source.account_id
        or source_info.get("namespace_id") != source.namespace_id
        or not isinstance(payload.get("records"), list)
    ):
        raise SystemExit(f"invalid or mismatched partial export: {path}")

    records = payload["records"]
    seen: set[str] = set()
    for record in records:
        key = str(record.get("key") or "")
        encoded = record.get("value_base64")
        if not key or not isinstance(encoded, str) or key in seen:
            raise SystemExit(f"invalid partial export record: {key!r}")
        seen.add(key)
        value = base64.b64decode(encoded, validate=True)
        if record.get("sha256") != record_digest(value):
            raise SystemExit(f"partial export checksum mismatch for key {key!r}")
    return records


def save_partial_export(
    path: Path,
    source: CloudflareKV,
    records: list[dict[str, Any]],
) -> None:
    write_json_atomic(
        path,
        {
            "format": "tongyi-cloudflare-kv-export-partial-v1",
            "source": {
                "account_id": source.account_id,
                "namespace_id": source.namespace_id,
            },
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "key_count": len(records),
            "records": records,
        },
    )


def load_and_validate_export(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    records = payload.get("records")
    if payload.get("format") != "tongyi-cloudflare-kv-export-v2" or not isinstance(records, list):
        raise SystemExit("invalid export file: expected tongyi-cloudflare-kv-export-v2")
    if payload.get("key_count") != len(records):
        raise SystemExit("invalid export file: key_count does not match records")

    seen: set[str] = set()
    for record in records:
        key = str(record.get("key") or "")
        encoded = record.get("value_base64")
        if not key or not isinstance(encoded, str) or key in seen:
            raise SystemExit(f"invalid export record: missing/duplicate key {key!r}")
        seen.add(key)
        try:
            value = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise SystemExit(f"invalid base64 for key {key!r}: {error}") from error
        if record.get("sha256") != record_digest(value):
            raise SystemExit(f"checksum mismatch in export file for key {key!r}")

    if payload.get("manifest_sha256") != manifest_digest(records):
        raise SystemExit("invalid export file: manifest checksum mismatch")
    return payload, records


def export_kv(args) -> None:
    source = build_source_client(args)
    output_path = Path(args.output).expanduser()
    partial_path = output_path.with_name(f"{output_path.name}.partial")
    include_prefixes = args.prefix or []
    exclude_prefixes = args.exclude_prefix or []

    keys = stable_key_list(source, include_prefixes, exclude_prefixes)
    initial_names = [str(item.get("name") or "") for item in keys if item.get("name")]
    initial_name_set = set(initial_names)
    records = load_partial_export(partial_path, source)
    records = [record for record in records if record["key"] in initial_name_set]
    completed_keys = {str(record["key"]) for record in records}
    if records:
        print(f"resuming from {len(records)} saved keys in {partial_path}", file=sys.stderr)
    started = time.time()
    for index, item in enumerate(keys, start=1):
        key = str(item.get("name") or "")
        if not key or key in completed_keys:
            continue
        value = source.get_value_bytes(key)
        if value is None:
            continue
        record = {
            "key": key,
            "value_base64": base64.b64encode(value).decode("ascii"),
            "sha256": record_digest(value),
        }
        if "metadata" in item and item.get("metadata") is not None:
            record["metadata"] = item.get("metadata")
        if "expiration" in item and item.get("expiration") is not None:
            record["expiration"] = item.get("expiration")
        records.append(record)
        completed_keys.add(key)
        # Save after every successful sequential read so a retry never needs to
        # spend the source read quota on already exported keys.
        save_partial_export(partial_path, source, records)
        if index % 50 == 0:
            print(f"exported {index}/{len(keys)} keys...", file=sys.stderr)

    records.sort(key=lambda record: record["key"])
    final_keys = stable_key_list(source, include_prefixes, exclude_prefixes)
    final_names = [str(item.get("name") or "") for item in final_keys if item.get("name")]
    exported_names = [record["key"] for record in records]
    if initial_names != final_names or initial_names != exported_names:
        raise SystemExit(
            "source KV changed during export; pause Worker writes and run export again"
        )

    payload = {
        "format": "tongyi-cloudflare-kv-export-v2",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {
            "account_id": source.account_id,
            "namespace_id": source.namespace_id,
        },
        "key_count": len(records),
        "excluded_prefixes": exclude_prefixes,
        "manifest_sha256": manifest_digest(records),
        "records": records,
    }
    write_json_atomic(output_path, payload)
    partial_path.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "export",
                "output": str(output_path),
                "keys": len(records),
                "excluded_prefixes": exclude_prefixes,
                "seconds": round(time.time() - started, 3),
            },
            ensure_ascii=False,
        )
    )


def import_kv(args) -> None:
    if not args.yes:
        raise SystemExit("refusing to import without --yes")
    target = build_target_client(args)
    input_path = Path(args.input).expanduser()
    payload, records = load_and_validate_export(input_path)
    source_info = payload.get("source") or {}
    if (
        source_info.get("account_id") == target.account_id
        and source_info.get("namespace_id") == target.namespace_id
    ):
        raise SystemExit("refusing to import back into the source KV namespace")

    started = time.time()
    imported = 0
    for index, record in enumerate(records, start=1):
        key = str(record.get("key") or "")
        encoded = record.get("value_base64")
        if not key or not isinstance(encoded, str):
            continue
        value = base64.b64decode(encoded)
        expiration = record.get("expiration")
        target.put_value_bytes(
            key,
            value,
            metadata=record.get("metadata"),
            expiration=int(expiration) if expiration is not None else None,
        )
        imported += 1
        if index % 50 == 0:
            print(f"imported {index}/{len(records)} keys...", file=sys.stderr)

    print(
        json.dumps(
            {
                "ok": True,
                "action": "import",
                "input": str(input_path),
                "target_account_id": target.account_id,
                "target_namespace_id": target.namespace_id,
                "keys": imported,
                "seconds": round(time.time() - started, 3),
            },
            ensure_ascii=False,
        )
    )


def check_export(args) -> None:
    input_path = Path(args.input).expanduser()
    payload, records = load_and_validate_export(input_path)
    print(
        json.dumps(
            {
                "ok": True,
                "action": "check",
                "input": str(input_path),
                "keys": len(records),
                "manifest_sha256": payload["manifest_sha256"],
            },
            ensure_ascii=False,
        )
    )


def verify_kv(args) -> None:
    target = build_target_client(args)
    input_path = Path(args.input).expanduser()
    _payload, records = load_and_validate_export(input_path)
    target_keys = {str(item.get("name") or ""): item for item in target.list_keys()}
    expected_names = {str(record["key"]) for record in records}
    checked = 0
    missing: list[str] = []
    different: list[str] = []
    metadata_different: list[str] = []
    expiration_different: list[str] = []

    for record in records:
        key = str(record.get("key") or "")
        encoded = record.get("value_base64")
        if not key or not isinstance(encoded, str):
            continue
        expected = base64.b64decode(encoded)
        actual = target.get_value_bytes(key)
        checked += 1
        if actual is None:
            missing.append(key)
        elif actual != expected:
            different.append(key)
        target_item = target_keys.get(key) or {}
        if target_item.get("metadata") != record.get("metadata"):
            metadata_different.append(key)
        if target_item.get("expiration") != record.get("expiration"):
            expiration_different.append(key)

    extra = sorted(set(target_keys) - expected_names) if args.exact else []
    result = {
        "ok": not missing and not different and not metadata_different
        and not expiration_different and not extra,
        "action": "verify",
        "checked": checked,
        "missing": missing,
        "different": different,
        "metadata_different": metadata_different,
        "expiration_different": expiration_different,
        "extra": extra,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


def quote_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(ch.isspace() for ch in value) or any(ch in value for ch in "#'\"\\$`"):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
        return f'"{escaped}"'
    return value


def env_export(args) -> None:
    source_values = parse_env_file(args.input_env)
    output_path = Path(args.output).expanduser()
    target_overrides = {
        "CF_ACCOUNT_ID": args.target_account_id,
        "CF_KV_NAMESPACE_ID": args.target_namespace_id,
        "CF_API_TOKEN": args.target_api_token,
    }

    lines: list[str] = [
        "# Generated by scripts/migrate_tongyi_kv.py env-export",
        "# This file is for the new server/account after Tongyi KV migration.",
        "# Cloudflare Worker secret values cannot be pulled back from Cloudflare;",
        "# fill the TODO items below and run wrangler secret put for each one.",
        "",
        "# Server / local sync environment",
    ]
    for key in SERVER_ENV_KEYS:
        value = str(target_overrides.get(key) or source_values.get(key) or os.getenv(key, "")).strip()
        if not value:
            lines.append(f"{key}=")
        else:
            lines.append(f"{key}={quote_env_value(value)}")

    lines.extend(
        [
            "",
            "# Tongyi Worker secrets to set in the target Cloudflare account.",
            "# These are placeholders because Cloudflare does not expose secret plaintext.",
        ]
    )
    for key in WORKER_SECRET_NAMES:
        local_value = str(source_values.get(key) or os.getenv(key, "")).strip()
        if local_value:
            lines.append(f"# {key} is present locally; set it with: wrangler secret put {key}")
        else:
            lines.append(f"# TODO: wrangler secret put {key}")

    lines.extend(
        [
            "",
            "# Optional migration helper variables",
            f"SOURCE_CF_ACCOUNT_ID={quote_env_value(str(source_values.get('CF_ACCOUNT_ID') or ''))}",
            f"SOURCE_CF_KV_NAMESPACE_ID={quote_env_value(str(source_values.get('CF_KV_NAMESPACE_ID') or ''))}",
            "SOURCE_CF_API_TOKEN=",
            f"TARGET_CF_ACCOUNT_ID={quote_env_value(args.target_account_id or '')}",
            f"TARGET_CF_KV_NAMESPACE_ID={quote_env_value(args.target_namespace_id or '')}",
            "TARGET_CF_API_TOKEN=",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "action": "env-export",
                "input": str(Path(args.input_env).expanduser()) if args.input_env else "",
                "output": str(output_path),
                "server_keys": len(SERVER_ENV_KEYS),
                "worker_secret_placeholders": len(WORKER_SECRET_NAMES),
            },
            ensure_ascii=False,
        )
    )


def add_common_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source-profile",
        choices=["source", "current", "new"],
        default="source",
        help="Environment variable set to use: SOURCE_CF_*, CF_*, or NEW_CF_*.",
    )
    parser.add_argument("--source-account-id", default="")
    parser.add_argument("--source-namespace-id", default="")
    parser.add_argument("--source-api-token", default="")


def add_common_target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target-account-id", default="")
    parser.add_argument("--target-namespace-id", default="")
    parser.add_argument("--target-api-token", default="")


def main() -> None:
    parser = argparse.ArgumentParser(description="Export/import Tongyi Cloudflare KV data between accounts")
    parser.add_argument("--env-file", default="", help="Optional env file to load before reading SOURCE_/TARGET_ variables")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export source KV to a local JSON file")
    add_common_source_args(export_parser)
    export_parser.add_argument("--output", default=str(DEFAULT_EXPORT_PATH))
    export_parser.add_argument(
        "--prefix",
        action="append",
        default=[],
        help="Only export keys with this prefix. Repeatable. Omit to export all keys.",
    )
    export_parser.add_argument(
        "--exclude-prefix",
        action="append",
        default=[],
        help="Exclude keys with this prefix. Repeatable. Omit to export every key.",
    )
    export_parser.set_defaults(func=export_kv)

    import_parser = subparsers.add_parser("import", help="Import a JSON export into target KV")
    add_common_target_args(import_parser)
    import_parser.add_argument("--input", default=str(DEFAULT_EXPORT_PATH))
    import_parser.add_argument("--yes", action="store_true", help="Required confirmation for writes")
    import_parser.set_defaults(func=import_kv)

    check_parser = subparsers.add_parser(
        "check",
        help="Validate every value checksum and the manifest without Cloudflare access",
    )
    check_parser.add_argument("--input", default=str(DEFAULT_EXPORT_PATH))
    check_parser.set_defaults(func=check_export)

    verify_parser = subparsers.add_parser("verify", help="Compare target KV values against a JSON export")
    add_common_target_args(verify_parser)
    verify_parser.add_argument("--input", default=str(DEFAULT_EXPORT_PATH))
    verify_parser.add_argument(
        "--exact",
        action="store_true",
        help="Also fail if the target contains keys absent from the export.",
    )
    verify_parser.set_defaults(func=verify_kv)

    env_parser = subparsers.add_parser("env-export", help="Generate a target env template from a local env file")
    env_parser.add_argument("--input-env", default=str(PROJECT_ROOT / "seat-qianduan.env.local"))
    env_parser.add_argument("--output", default=str(DEFAULT_ENV_EXPORT_PATH))
    env_parser.add_argument("--target-account-id", default="")
    env_parser.add_argument("--target-namespace-id", default="")
    env_parser.add_argument("--target-api-token", default="")
    env_parser.set_defaults(func=env_export)

    args = parser.parse_args()
    load_env_file(args.env_file)
    args.func(args)


if __name__ == "__main__":
    main()
