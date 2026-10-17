"""本地预览导航（仅开发用）：用隔离的临时部署目录 + 临时管理账号起 WebUI，方便截图看 UI。

不读写项目真实的 rooms/、data/、accounts/。

用法: ./.venv/Scripts/python.exe scripts/preview_ui.py [端口]
     默认账号 preview / preview123
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 19896
USER = "preview"
PASS = "preview123"
SCRATCH = Path("C:/Users/shinshi/AppData/Local/hermes/cache/scratch/preview_root")
ROOM_ID = "1946287911"


def prepare() -> str:
    """搭一个隔离的迷你部署目录：rooms/<id>/config.yaml + 临时 web_ui 账号。"""
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    room_dir = SCRATCH / "rooms" / ROOM_ID
    (room_dir / "data").mkdir(parents=True, exist_ok=True)

    src = yaml.safe_load((ROOT / "rooms" / ROOM_ID / "config.yaml").read_text(encoding="utf-8"))
    src["web_ui"] = dict(src.get("web_ui") or {}, username=USER, password=PASS, api_token="")
    src["storage"] = {"sqlite_path": "data/bot.db"}
    src["auth"] = dict(src.get("auth") or {}, credential_store_path="data/credential.json")
    cfg = room_dir / "config.yaml"
    cfg.write_text(yaml.safe_dump(src, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(cfg)


def main() -> None:
    import uvicorn

    from app.config import load_config
    from app.web import server as srv

    cfg_path = prepare()
    srv.init_app(load_config(cfg_path), config_path=cfg_path)
    print(f"preview: http://127.0.0.1:{PORT}/  账号 {USER} / 密码 {PASS}", flush=True)
    uvicorn.run(srv.app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

