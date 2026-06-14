#!/usr/bin/env python3
"""
Ayabot 管理员密码重置工具
────────────────────────
用法：
    python -m app.reset_admin                    # 重置为随机密码（打印到终端）
    python -m app.reset_admin --password mypass  # 重置为指定密码
    python -m app.reset_admin --username myadmin # 重置指定用户（默认 ayabot）
    python -m app.reset_admin --no-reset-flag    # 不添加 must_reset_password 标记
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import string
import sys
from pathlib import Path


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _find_users_file() -> Path:
    """从项目根目录查找 data/users.json。"""
    candidates = [
        Path("data/users.json"),
        Path(__file__).resolve().parent.parent / "data" / "users.json",
    ]
    for p in candidates:
        if p.exists():
            return p
    # 不存在则创建
    p = candidates[-1]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def generate_password(length: int = 12) -> str:
    """生成随机密码。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def reset_admin(
    username: str = "ayabot",
    password: str | None = None,
    must_reset: bool = True,
) -> str:
    """重置管理员密码。返回新密码。"""
    if password is None:
        password = generate_password()

    users_file = _find_users_file()
    users: dict = {}
    if users_file.exists():
        try:
            users = json.loads(users_file.read_text(encoding="utf-8"))
        except Exception:
            users = {}

    users[username] = {
        "password_hash": _hash_password(password),
        "role": "admin",
        "allowed_rooms": [],
    }
    if must_reset:
        users[username]["must_reset_password"] = True

    users_file.write_text(
        json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"✅ 用户 '{username}' 密码已重置")
    print(f"   新密码: {password}")
    if must_reset:
        print(f"   下次登录将强制要求修改密码")
    print(f"   文件: {users_file.resolve()}")
    return password


def main() -> None:
    parser = argparse.ArgumentParser(description="Ayabot 管理员密码重置工具")
    parser.add_argument(
        "--password", "-p", type=str, default=None,
        help="指定新密码（不指定则自动生成随机密码）",
    )
    parser.add_argument(
        "--username", "-u", type=str, default="ayabot",
        help="要重置的用户名（默认: ayabot）",
    )
    parser.add_argument(
        "--no-reset-flag", action="store_true",
        help="不添加 must_reset_password 标记（首次登录不强制改密码）",
    )
    args = parser.parse_args()

    reset_admin(
        username=args.username,
        password=args.password,
        must_reset=not args.no_reset_flag,
    )


if __name__ == "__main__":
    main()
