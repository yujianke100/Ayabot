"""
Ayabot PyInstaller 构建脚本（Windows）
───────────────────────────────────
用法（在 Windows 上）：
  1. pip install pyinstaller pystray pillow
  2. cd ayabot
  3. python scripts\build_exe.py

输出: dist\Ayabot.exe (单文件，含托盘支持)
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    entry = base_dir / "scripts" / "tray_win.py"
    icon_path = base_dir / "logo.png"

    if not entry.exists():
        sys.exit(f"ERROR: entry point not found: {entry}")

    cmd = [
        "pyinstaller",
        "--onefile",                # 单 .exe 文件
        "--windowed",               # 无控制台窗口（后台托盘）
        "--name", "Ayabot",
        "--add-data", f"{base_dir / 'app'}{os.pathsep}app",
        "--hidden-import", "app.main",
        "--hidden-import", "app.bot",
        "--hidden-import", "app.web.server",
        "--hidden-import", "app.config",
        "--hidden-import", "app.storage",
        "--hidden-import", "app.auth",
        "--hidden-import", "app.llm_client",
        "--hidden-import", "uvicorn",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "yaml",
        "--hidden-import", "aiohttp",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--collect-all", "bilibili_api",
        "--collect-all", "pystray",
    ]

    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])

    cmd.append(str(entry))

    print(f"Building Ayabot.exe...")
    print(f"Entry: {entry}")
    print(f"Command: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=base_dir)
    if result.returncode == 0:
        print()
        print("✅ Build successful!")
        print(f"   Output: {base_dir / 'dist' / 'Ayabot.exe'}")
        print()
        print("📦 分发时附带:")
        print(f"   - config.yaml（放到 %USERPROFILE%\\.ayabot\\ 下）")
        print(f"   - 用户首次运行会自动从 .exe 同级目录复制 config.yaml")
    else:
        print(f"❌ Build failed (exit code {result.returncode})")


if __name__ == "__main__":
    import os  # noqa: PLC0415
    main()
