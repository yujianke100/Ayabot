"""单源版本号管理 — 所有地方都从 VERSION 文件读取。"""

from pathlib import Path


def get_version() -> str:
    """从项目根目录 VERSION 文件读取版本号。"""
    ver_path = Path(__file__).resolve().parent.parent / "VERSION"
    return ver_path.read_text(encoding="utf-8").strip()


def get_version_display() -> str:
    """带 v 前缀的版本号展示，如 v0.1.2。"""
    return "v" + get_version()
