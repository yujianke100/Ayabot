"""弹幕 XML 导入的坏输入矩阵 + 请求体上限。

这是用户最初要的功能，真实世界的文件千奇百怪：从浏览器另存、第三方工具导出、
解压坏的、GBK 编码、BOM、空文件、只有一小段、`.xml` 其实是 HTML/JSON。

要求：
1. **任何坏输入都必须给出可读的中文错误**，绝不能 5xx；
2. 必须是 **4xx**（客户端错误），不能是「200 + {"ok": false}」——
   那让任何用 `res.ok` 判断的调用方把「文件不能用」当成导入成功
   （第 23 轮在配置保存上修过同一个病根）；
3. 导入接口的请求体上限必须是 `danmaku_xml.MAX_XML_BYTES`（64MB），
   不是全局的 8MB —— 一场几小时的录播弹幕就是 20–50MB。

    ./.venv/Scripts/python.exe scripts/check_import_bad_inputs.py
"""
from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 18961
ROOM = "1946287911"
TOKEN = "xml-bad"

GOOD_HEAD = ('<?xml version="1.0" encoding="UTF-8"?><i><chatserver>chat.bilibili.com</chatserver>'
             '<chatid>1946287911</chatid><mission>0</mission><maxlimit>1000</maxlimit>'
             '<state>0</state><real_name>0</real_name><source>k-v</source>')
CHAT_ITEM = '<d p="1718000000.000,1,25,16777215,1718000000,0,abc,1234567890,0">你好</d>'

# (标签, 负载, 期望状态码)
CASES = [
    ("空文件", b"", 400),
    ("纯文本", "这不是 XML，只是一段文字".encode("utf-8"), 422),
    ("JSON 冒充 XML", b'{"code": 0, "data": []}', 422),
    ("HTML 页面", b"<!DOCTYPE html><html><body>404</body></html>", 422),
    ("截断的 XML", (GOOD_HEAD + CHAT_ITEM)[:-30].encode("utf-8"), 422),
    ("只有普通弹幕（没有礼物）", (GOOD_HEAD + CHAT_ITEM * 3 + "</i>").encode("utf-8"), 422),
    ("GBK 编码", (GOOD_HEAD + CHAT_ITEM + "</i>").encode("gbk"), 422),
    ("带 BOM", b"\xef\xbb\xbf" + (GOOD_HEAD + CHAT_ITEM + "</i>").encode("utf-8"), 422),
    ("<d> 缺 p 属性", (GOOD_HEAD + "<d>没有属性</d></i>").encode("utf-8"), 422),
    ("p 属性字段不够", (GOOD_HEAD + '<d p="1,2">短</d></i>').encode("utf-8"), 422),
]


def main() -> int:
    import uvicorn
    from app.web import server as srv
    from app.danmaku_xml import MAX_XML_BYTES

    tmp = Path(tempfile.mkdtemp(prefix="xmlbad_"))
    fails: list[str] = []

    def ok(label: str, cond: bool, extra="") -> None:
        print(f"  {'OK  ' if cond else 'FAIL'} {label}" + (f" -> {extra}" if extra else ""))
        if not cond:
            fails.append(label)

    try:
        (tmp / "data").mkdir(parents=True)
        example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
        (tmp / "config.yaml").write_text(example, encoding="utf-8")
        room = tmp / "rooms" / ROOM
        (room / "data").mkdir(parents=True)
        (room / "config.yaml").write_text(example, encoding="utf-8")

        srv._ROOMS_BASE_DIR = str(tmp)
        srv._ROOM_DB_PATH_CACHE.clear()
        srv._DB_PATH = str(tmp / "data" / "bot.db")
        srv._CONFIG_YAML_PATH = str(tmp / "config.yaml")
        srv._API_TOKEN = TOKEN
        srv._SESSIONS[TOKEN] = (time.time() + 3600, "t", "admin", [])
        srv._rate_limited = lambda ip: False  # type: ignore[assignment]
        srv._GIFT_CACHE_BUILT = True
        srv._GIFT_PRICE_CACHE = {"辣条": (100, "silver")}

        cfg = uvicorn.Config(srv.app, host="127.0.0.1", port=PORT, log_level="critical")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        for _ in range(120):
            if getattr(server, "started", False):
                break
            time.sleep(0.1)

        url = f"http://127.0.0.1:{PORT}/api/rooms/{ROOM}/import/danmaku-xml?dry_run=1"

        print("[1] 坏输入矩阵：必须是可读的 4xx，不能 5xx、不能 200+ok:false")
        for label, payload, expect in CASES:
            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Cookie", f"session={TOKEN}")
            req.add_header("Content-Type", "application/xml")
            req.add_header("X-Filename", "probe.xml")
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    code, text = r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                code, text = exc.code, exc.read().decode("utf-8", "replace")
            try:
                body = json.loads(text)
            except Exception:
                body = {"_raw": text[:120]}
            msg = str(body.get("error") or "")[:70]
            got_error = bool(body.get("error"))
            ok(f"{label:22s}", code == expect and got_error, f"HTTP {code} {msg}")
            if not got_error:
                fails.append(f"{label}: 没有给出 error 文案")

        print("[2] 请求体上限必须是 64MB（不是全局 8MB）")
        # 只发头不发 body：中间件按 Content-Length 判断上限，几秒出结果
        for mb in (20, 50):
            size = mb * 1024 * 1024
            c = socket.create_connection(("127.0.0.1", PORT), timeout=10)
            c.sendall((f"POST /api/rooms/{ROOM}/import/danmaku-xml HTTP/1.1\r\n"
                       f"Host: 127.0.0.1\r\nCookie: session={TOKEN}\r\n"
                       f"Content-Type: application/xml\r\nX-Filename: big.xml\r\n"
                       f"Content-Length: {size}\r\nConnection: close\r\n\r\n").encode())
            c.settimeout(6)
            try:
                head = c.recv(200).decode("utf-8", "replace").split("\r\n")[0]
            except socket.timeout:
                head = "HTTP/1.1 (服务器在等 body)"
            c.close()
            ok(f"声明 {mb} MB 不被 413 挡住", "413" not in head, head)

        ok("XML 模块的上限确实是 64MB", MAX_XML_BYTES == 64 * 1024 * 1024,
           f"{MAX_XML_BYTES // 1048576} MB")

        print("[3] 超过 64MB 必须明确拒绝（413，不是 OOM 或静默截断）")
        too_big = MAX_XML_BYTES + 1
        c = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        c.sendall((f"POST /api/rooms/{ROOM}/import/danmaku-xml HTTP/1.1\r\n"
                   f"Host: 127.0.0.1\r\nCookie: session={TOKEN}\r\n"
                   f"Content-Type: application/xml\r\nX-Filename: big.xml\r\n"
                   f"Content-Length: {too_big}\r\nConnection: close\r\n\r\n").encode())
        c.settimeout(6)
        try:
            head = c.recv(200).decode("utf-8", "replace").split("\r\n")[0]
        except socket.timeout:
            head = "HTTP/1.1 (服务器在等 body)"
        c.close()
        ok("65MB 被 413 拒绝", "413" in head, head)

        server.should_exit = True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED {len(fails)} 项: {fails[:6]}")
        return 1
    print("IMPORT BAD INPUT OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
