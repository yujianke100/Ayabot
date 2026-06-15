"""Ayabot PyInstaller 构建脚本（Windows）."""
import subprocess
import sys
from pathlib import Path
import os


def _make_ico(png_path: Path) -> Path | None:
    """用 PIL 将 PNG 转成 ICO（多个标准尺寸，Windows 图标更清晰）。"""
    ico_path = png_path.with_suffix(".ico")
    try:
        from PIL import Image
        img = Image.open(png_path)
        # 保留宽高比缩放到 256x256
        img = img.resize((256, 256), Image.LANCZOS)
        # 保存多个标准尺寸供 Windows 在不同视图下选择
        img.save(ico_path, format="ICO", sizes=[(256, 256), (48, 48), (32, 32), (16, 16)])
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
        "-p", str(base_dir),
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

    cmd.append(str(entry))

    print(f"Building Ayabot.exe...")
    result = subprocess.run(cmd, cwd=base_dir)

    # 构建完成后清理临时 .ico
    for ico in base_dir.glob("*.ico"):
        try:
            ico.unlink()
        except Exception:
            pass

    if result.returncode == 0:
        print(f"\n✅ Build successful! Output: {base_dir / 'dist' / 'Ayabot.exe'}")
        print(f"💡 如果 .exe 图标没刷新，请尝试：")
        print(f"   1. 把 .exe 复制到新目录再查看")
        print(f"   2. 重启 Windows 资源管理器 (任务管理器 → Windows 资源管理器 → 重新启动)")
        print(f"   3. 或运行: ie4uinit.exe -Show")
    else:
        print(f"❌ Build failed (exit code {result.returncode})")


if __name__ == "__main__":
    main()
