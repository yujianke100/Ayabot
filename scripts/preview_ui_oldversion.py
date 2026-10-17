"""预览：模拟一个「落后版本」的部署，验证新版本提醒 UI。

用法: python scripts/preview_ui_oldversion.py [端口]
     账号 preview / preview123，版本假装是 v1.1.0（最新是 v1.2.0 → 会提示更新）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19895


def main() -> None:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    cfg_path = str(ROOT / "scripts" / ".." / "rooms" / "1946287911" / "config.yaml")
    # 复用隔离预览环境（临时目录 + 临时账号），只把版本号伪装成旧的
    import scripts.preview_ui as preview  # noqa: PLC0415

    cfg_path = preview.prepare()
    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    srv.get_version_display = lambda: "v1.1.0"  # type: ignore[assignment]
    print(f"preview(old version): http://127.0.0.1:{PORT}/  账号 {preview.USER} / 密码 {preview.PASS}", flush=True)
    uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
