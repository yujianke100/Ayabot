"""本地 T2I 服务 — 将 HTML 渲染为图片，替代 soulter.top 外部服务。"""
import asyncio
import base64
import os
from pathlib import Path

from fastapi import FastAPI, Form
from fastapi.responses import Response
import uvicorn
from playwright.async_api import async_playwright

app = FastAPI(title="Local T2I")
_browser = None
_semaphore = asyncio.Semaphore(2)  # 最多 2 个并发渲染


@app.on_event("startup")
async def startup():
    global _browser
    p = await async_playwright().start()
    _browser = await p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )


@app.post("/text2img")
async def text2img(html: str = Form(...), css: str = Form("")):
    if not html:
        return {"error": "html is required"}
    async with _semaphore:
        context = await _browser.new_context(
            viewport={"width": 400, "height": 10},
            device_scale_factor=2,  # 2x 高清
        )
        page = await context.new_page()
        try:
            # 注入 CSS
            if css:
                html = f"<style>{css}</style>{html}"
            await page.set_content(html, wait_until="networkidle")
            # 等待图片加载
            await page.wait_for_timeout(500)
            # 截图
            img_bytes = await page.screenshot(full_page=True, type="png")
            return Response(content=img_bytes, media_type="image/png")
        finally:
            await page.close()
            await context.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("T2I_PORT", "6199"))
    uvicorn.run(app, host="0.0.0.0", port=port)
