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


def _load_version() -> str:
    """从项目根目录 VERSION 文件读取版本号。"""
    base_dir = Path(__file__).resolve().parent.parent
    ver_file = base_dir / "VERSION"
    if ver_file.exists():
        return ver_file.read_text(encoding="utf-8").strip()
    return ""


def _write_version_file(ver: str, out_path: Path) -> Path | None:
    """生成 PyInstaller 的 --version-file，让 exe 带公司/产品/版权信息。

    有了它，文件属性里能看到「Ayabot / 版本 / 版权」而不是一片空白；
    申请代码签名证书时也需要这些字段。
    """
    import re

    nums = [int(x) for x in re.findall(r"\d+", ver)][:3] or [0, 0, 0]
    while len(nums) < 3:
        nums.append(0)
    quad = (nums[0], nums[1], nums[2], 0)
    content = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad!r},
    prodvers={quad!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Ayabot'),
        StringStruct('FileDescription', 'Ayabot B站直播间机器人'),
        StringStruct('FileVersion', '{ver}'),
        StringStruct('InternalName', 'Ayabot'),
        StringStruct('LegalCopyright', 'GPL-3.0-only'),
        StringStruct('OriginalFilename', 'Ayabot-v{ver}.exe'),
        StringStruct('ProductName', 'Ayabot 直播间机器人'),
        StringStruct('ProductVersion', '{ver}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    try:
        out_path.write_text(content, encoding="utf-8")
        return out_path
    except OSError as exc:
        print(f"[WARN] could not write version file: {exc}")
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Ayabot Windows .exe")
    parser.add_argument("--version", default="", help="版本号, e.g. 0.1.2（默认从 VERSION 文件读取）")
    args = parser.parse_args()

    # 优先用命令行参数，否则从 VERSION 文件读取
    ver = args.version or _load_version()
    ver = ver.lstrip("v")  # 去掉 "v" 前缀
    exe_name = f"Ayabot-v{ver}" if ver else "Ayabot"

    base_dir = Path(__file__).resolve().parent.parent
    entry = base_dir / "scripts" / "tray_win.py"
    icon_png = base_dir / "figs" / "icon.png"
    logo_png = base_dir / "figs" / "logo.png"

    if not entry.exists():
        sys.exit(f"ERROR: entry point not found: {entry}")

    # 用当前解释器的 -m PyInstaller：venv/uv 里的 pyinstaller.exe 有时是不可用的跳板
    cmd = [
        sys.executable, "-m", "PyInstaller",
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
    elif logo_png.exists():
        ico = _make_ico(logo_png)
        if ico:
            cmd.extend(["--icon", str(ico)])
        else:
            cmd.extend(["--icon", str(logo_png)])

    # 打包 figs/ 目录下的图片（打赏二维码、logo、icon 等）
    for fname in ["alipay.jpg", "wechat.png", "icon.png", "logo.png"]:
        fp = base_dir / "figs" / fname
        if fp.exists():
            cmd.extend(["--add-data", f"{fp}{os.pathsep}figs"])

    # 打包 VERSION 文件（供 app/version.py 在冻结模式下读取）
    ver_file = base_dir / "VERSION"
    if ver_file.exists():
        cmd.extend(["--add-data", f"{ver_file}{os.pathsep}."])

    # 打包默认配置（嵌入 exe，运行时自动释放到 DATA_DIR）
    example_cfg = base_dir / "config.example.yaml"
    if example_cfg.exists():
        cmd.extend(["--add-data", f"{example_cfg}{os.pathsep}."])

    # exe 版本资源（文件属性里的公司/产品/版权，签名证书也依赖它）
    ver_file_out = _write_version_file(ver, base_dir / "build_version_info.txt")
    if ver_file_out:
        cmd.extend(["--version-file", str(ver_file_out)])

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
