"""Ayabot PyInstaller 构建脚本（Windows）."""
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
        "--onefile",
        "--windowed",
        "--name", "Ayabot",
        "--add-data", f"{base_dir / 'app'}{os.pathsep}app",
        "--hidden-import", "app.main",
        "--hidden-import", "app.bot",
        "--hidden-import", "app.web.server",
        "--hidden-import", "app.config",
        "--hidden-import", "app.storage",
        "--hidden-import", "app.auth",
        "--hidden-import", "app.llm_client",
        "--hidden-import", "app.process_manager",
        "--hidden-import", "app.reset_admin",
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
    result = subprocess.run(cmd, cwd=base_dir)
    if result.returncode == 0:
        print(f"\n✅ Build successful! Output: {base_dir / 'dist' / 'Ayabot.exe'}")
    else:
        print(f"❌ Build failed (exit code {result.returncode})")


if __name__ == "__main__":
    main()
