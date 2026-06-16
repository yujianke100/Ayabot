"""Ayabot PyInstaller 构建脚本（Windows）."""
import argparse
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
        print(f"[OK] Converted {png_path.name} -> {ico_path.name}")
        return ico_path
    except Exception as exc:
        print(f"[WARN] Could not convert to ICO: {exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Ayabot Windows .exe")
    parser.add_argument("--version", default="", help="版本号, e.g. 0.1.1")
    args = parser.parse_args()

    exe_name = "Ayabot"
    if args.version:
        ver = args.version.lstrip("v")  # 去掉 "v" 前缀
        exe_name = f"Ayabot-v{ver}"

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
        "--name", exe_name,
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

    # 打包默认配置（嵌入 exe，运行时自动释放到 DATA_DIR）
    example_cfg = base_dir / "config.example.yaml"
    if example_cfg.exists():
        cmd.extend(["--add-data", f"{example_cfg}{os.pathsep}."])

    cmd.append(str(entry))

    print(f"Building {exe_name}.exe...")
    result = subprocess.run(cmd, cwd=base_dir)

    # 构建完成后清理临时 .ico
    for ico in base_dir.glob("*.ico"):
        try:
            ico.unlink()
        except Exception:
            pass

    if result.returncode == 0:
        print(f"\n[OK] Build successful! Output: {base_dir / 'dist' / (exe_name + '.exe')}")
        print(f"[TIP] If .exe icon does not refresh, try:")
        print(f"   1. Copy .exe to a new directory")
        print(f"   2. Restart Windows Explorer (Task Manager -> Windows Explorer -> Restart)")
        print(f"   3. Run: ie4uinit.exe -Show")
    else:
        print(f"[FAIL] Build failed (exit code {result.returncode})")


if __name__ == "__main__":
    main()
