"""单源版本号管理 — 所有地方都从 VERSION 文件读取。"""

import sys
from pathlib import Path


def _find_version_file() -> Path:
    """查找 VERSION 文件：PyInstaller 冻结模式 → sys._MEIPASS，否则按项目路径。"""
    # PyInstaller 冻结环境
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        p = Path(sys._MEIPASS) / "VERSION"
        if p.exists():
            return p
    # 正常 Python 环境：相对 __file__
    p = Path(__file__).resolve().parent.parent / "VERSION"
    if p.exists():
        return p
    # 兜底
    return p


def get_version() -> str:
    """从 VERSION 文件读取版本号。"""
    return _find_version_file().read_text(encoding="utf-8").strip()


def get_version_display() -> str:
    """带 v 前缀的版本号展示，如 v0.1.2。"""
    return "v" + get_version()
