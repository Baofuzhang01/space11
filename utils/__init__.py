import json
import os
from pathlib import Path
from .encrypt import AES_Encrypt, AES_Decrypt, generate_captcha_key, enc, verify_param
from .reserve import reserve


def _get_utils_config_path():
    return Path(__file__).with_name("config.json")


def _load_utils_config():
    config_path = _get_utils_config_path()
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _iter_github_account_keys(config):
    """按稳定顺序返回 utils/config.json 中的 GitHub 账号键名。"""
    github_keys = []
    if "github" in config and isinstance(config["github"], dict):
        github_keys.append("github")

    suffix_pairs = []
    for key, value in config.items():
        if key == "github" or not isinstance(value, dict):
            continue
        if not key.startswith("github"):
            continue
        suffix = key[len("github"):]
        if suffix.isdigit():
            suffix_pairs.append((int(suffix), key))

    suffix_pairs.sort(key=lambda item: item[0])
    github_keys.extend(key for _, key in suffix_pairs)
    return github_keys


def get_github_accounts(include_incomplete=False):
    """读取 utils/config.json 中的 github/github2/github3... 账号配置。"""
    config = _load_utils_config()
    accounts = []
    for index, key in enumerate(_iter_github_account_keys(config), start=1):
        account = dict(config.get(key) or {})
        normalized = {
            "index": index,
            "key": key,
            "label": str(account.get("label", "")).strip(),
            "username": str(account.get("username", "")).strip(),
            "token": str(account.get("token", "")).strip(),
            "repo_name": str(account.get("repo_name", "")).strip(),
        }
        if include_incomplete or any(normalized[field] for field in ["label", "username", "token", "repo_name"]):
            accounts.append(normalized)
    return accounts


def get_github_account(identifier=None, include_incomplete=False):
    """按编号、键名、label 或 username 获取单个 GitHub 账号配置。

    兼容:
    - 1 / "1" -> github
    - 2 / "2" -> github2
    - 3 / "3" -> github3
    - "github3" -> github3
    - "githubm" 或 label / username -> 对应账号
    """
    accounts = get_github_accounts(include_incomplete=include_incomplete)
    if identifier is None:
        return accounts[0] if accounts else None

    ident = str(identifier).strip()
    if not ident:
        return accounts[0] if accounts else None

    if ident.isdigit():
        target_index = int(ident)
        for account in accounts:
            if account["index"] == target_index:
                return account
        return None

    ident_lower = ident.lower()
    for account in accounts:
        key_lower = account["key"].lower()
        label_lower = account["label"].lower()
        username_lower = account["username"].lower()
        if ident_lower in {key_lower, label_lower, username_lower}:
            return account
    return None

def _fetch_env_variables(env_name, action):
    try:
        return os.environ[env_name] if action else ""
    except KeyError:
        print(f"Environment variable {env_name} is not configured correctly.")
        return None

def get_user_credentials(action):
    """在 GitHub Actions(--action) 模式下优先使用环境变量账号.

    优先级:
    1. CX_USERNAME / CX_PASSWORD (你之前一直在用的变量名)
    2. USERNAMES / PASSWORDS (兼容旧配置, 允许逗号分隔多账号)

    本地不带 --action 运行时, 仍然使用 config.json 里的 username/password.
    """
    if not action:
        # 本地模式直接用 config.json 里的用户名/密码
        return "", ""

    # 1. 优先使用 CX_USERNAME / CX_PASSWORD
    cx_username = os.environ.get("CX_USERNAME")
    cx_password = os.environ.get("CX_PASSWORD")
    if cx_username and cx_password:
        return cx_username, cx_password

    # 2. 兼容旧的 USERNAMES / PASSWORDS（支持逗号分隔多账号）
    usernames = _fetch_env_variables("USERNAMES", action)
    passwords = _fetch_env_variables("PASSWORDS", action)
    return usernames, passwords
