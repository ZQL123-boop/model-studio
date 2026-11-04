"""给 README 生成界面截图（Playwright 无头浏览器）。

用法（项目根目录）：
    .venv/bin/python scripts/shot_ui.py

产物：docs/screenshots/*.png
前提：8600 后端在跑；模型已启动（对话截图需要）；algo-rag 服务在跑（知识库截图需要）。
门禁若开着，脚本会用 sessionStorage 绕过，不改后端状态。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
BASE = "http://127.0.0.1:8600"

# 跳过门禁登录层 + 固定一组生成参数，保证截图稳定
INIT_JS = """
try {
  sessionStorage.setItem('ms_gate_ok', '1');
  localStorage.setItem('ms_gen_settings', JSON.stringify({
    temperature: 0.7, top_p: 0.9, max_tokens: 512, thinking: false
  }));
} catch (e) {}
"""


def shot(page, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    page.screenshot(path=str(p))
    print(f"  saved {p.relative_to(ROOT)}  ({p.stat().st_size // 1024} KB)", flush=True)


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        page = ctx.new_page()
        page.add_init_script(INIT_JS)
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1800)

        # ---------------------------------------------------------- 1. 对话
        try:
            print("[1/6] 对话…", flush=True)
            page.fill(".composer textarea", "中山天气怎么样")
            page.click(".composer .row button")
            # 等按钮从「生成中…」变回「发送」
            page.wait_for_function(
                """() => {
                  const b = document.querySelector('.composer .row button');
                  return b && !b.disabled;
                }""",
                timeout=300_000,
            )
            page.wait_for_timeout(1200)
            shot(page, "01-chat.png")
        except Exception as e:  # noqa: BLE001
            print(f"  对话截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            shot(page, "01-chat.png")

        # ---------------------------------------------------------- 2. 知识库
        try:
            print("[2/6] 知识库…", flush=True)
            page.click(".tab:has-text('知识库')")
            page.wait_for_timeout(600)
            page.fill(".composer textarea", "为什么树状数组节点管的区间长度等于 lowbit？")
            page.click(".composer .row button")
            page.wait_for_selector(".ans-text", timeout=360_000)
            page.wait_for_timeout(900)
            shot(page, "02-rag.png")
        except Exception as e:  # noqa: BLE001
            print(f"  知识库截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            shot(page, "02-rag.png")

        # ---------------------------------------------------------- 3. 回放
        try:
            print("[3/6] 回放…", flush=True)
            page.click(".subtab:has-text('回放')")
            page.wait_for_selector(".replay-row", timeout=60_000)
            page.click(".replay-row")
            page.wait_for_selector(".replay-head", timeout=60_000)
            page.wait_for_timeout(900)
            shot(page, "03-replay.png")
        except Exception as e:  # noqa: BLE001
            print(f"  回放截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            shot(page, "03-replay.png")

        # ---------------------------------------------------------- 4. 记忆
        try:
            print("[4/6] 记忆…", flush=True)
            page.click(".tab:has-text('记忆')")
            page.wait_for_timeout(1500)
            page.screenshot(path=str(OUT / "04-memory.png"), full_page=False)
            print("  saved docs/screenshots/04-memory.png", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  记忆截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)

        # ---------------------------------------------------------- 5. 设计
        try:
            print("[5/6] 设计…", flush=True)
            page.click(".tab:has-text('设计')")
            page.wait_for_timeout(700)
            page.fill(".design-bar input", "~/Projects/model-studio/web/dist/index.html")
            page.click("button:has-text('加载')")
            page.wait_for_timeout(1800)
            shot(page, "05-design.png")
        except Exception as e:  # noqa: BLE001
            print(f"  设计截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            shot(page, "05-design.png")

        # ---------------------------------------------------------- 6. 设置
        try:
            print("[6/6] 设置抽屉…", flush=True)
            page.click("button:has-text('设置')")
            page.wait_for_timeout(800)
            shot(page, "06-settings.png")
        except Exception as e:  # noqa: BLE001
            print(f"  设置截图失败：{type(e).__name__}: {e}", file=sys.stderr, flush=True)

        browser.close()
        print("done.", flush=True)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"耗时 {time.time() - t0:.1f}s", flush=True)
