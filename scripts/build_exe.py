"""Ayabot PyInstaller 构建脚本（Windows）."""
import subprocess
import sys
from pathlib import Path
import os


def _make_ico(png_path: Path) -> Path | None:
    """用 PIL 将 PNG 转成 ICO（PyInstaller --icon 在 Windows 上对 ICO 最可靠）。"""
    ico_path = png_path.with_suffix(".ico")
    try:
        from PIL import Image
        img = Image.open(png_path)
        # ICO 需要 256x256 或更小
        if max(img.size) > 256:
            img = img.resize((256, 256))
        img.save(ico_path, format="ICO", sizes=[(256, 256)])
        print(f"✅ Converted {png_path.name} -> {ico_path.name}")
        return ico_path
    except Exception as exc:
        print(f"⚠️  Could not convert to ICO: {exc}")
        return None


def main() -> None:
    base_dir = Path(__file__).resolve().parent.parent
    entry = base_dir / "scripts" / "tray_win.py"
    icon_png = base_dir / "icon.png"
    logo_png = base_dir / "logo.png"

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
        "--hidden-import", "fastapi",
        "--hidden-import", "starlette",
        "--hidden-import", "pydantic",
        "--hidden-import", "yaml",
        "--hidden-import", "aiohttp",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "PIL.ImageDraw",
        "--hidden-import", "sqlite3",
        "--hidden-import", "_sqlite3",
        "--collect-all", "bilibili_api",
        "--collect-all", "pystray",
    ]

    # ── 图标打包 ──
    # 打包成 ICO 给 --icon（Windows 上最可靠），同时把 PNG 放进 bundle 给托盘用
    if icon_png.exists():
        ico = _make_ico(icon_png)
        if ico:
            cmd.extend(["--icon", str(ico)])
        else:
            cmd.extend(["--icon", str(icon_png)])
        cmd.extend(["--add-data", f"{icon_png}{os.pathsep}."])
    elif logo_png.exists():
        ico = _make_ico(logo_png)
        if ico:
            cmd.extend(["--icon", str(ico)])
        else:
            cmd.extend(["--icon", str(logo_png)])
        cmd.extend(["--add-data", f"{logo_png}{os.pathsep}."])

    print(f"Building Ayabot.exe...")
    result = subprocess.run(cmd, cwd=base_dir)
    if result.returncode == 0:
        print(f"\n✅ Build successful! Output: {base_dir / 'dist' / 'Ayabot.exe'}")
    else:
        print(f"❌ Build failed (exit code {result.returncode})")


if __name__ == "__main__":
    main()
