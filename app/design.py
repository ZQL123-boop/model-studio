"""界面视觉设计：无头浏览器截图 + 布局问题检测。

## 为什么用无头浏览器而不是正则解析 HTML

排版问题（截断、重叠、超出屏幕）**只有真正渲染之后才存在**。
静态分析 HTML 只能看出标签结构，看不出 1440px 下这行字会不会被切掉。

## 检测什么

按"用户能看见的问题"排优先级：
1. 文字截断（overflow:hidden 且 scrollWidth > clientWidth）
2. 页面横向溢出（scrollWidth > 视口）
3. 图片没加载 / 比例变形
4. 点击区偏小（< 44×44，移动端手指点不准）
5. 固定元素遮挡（position:fixed 高度超过视口 50%）
6. 文字重叠（两个可见元素的 bbox 相交）
7. 对比度不足（前景/背景色的 WCAG 对比度 < 3）
8. 脚本报错（console.error / pageerror）

每条都带 CSS 选择器，方便定位到具体元素。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Chromium 装在项目内的 sandbox 目录（系统缓存目录的 __dirlock 删不掉，
# 装到 Library/Caches 会一直失败）。这里显式指定，Playwright 才找得到。
_PW_PATH = Path.home() / "Projects" / "sandbox" / ".pw-browsers"
if _PW_PATH.exists():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_PW_PATH))

# 注入页面的检测脚本。返回 [{type, detail, sel, level}]
_JS_CHECK = r"""
() => {
  const out = [];
  const vw = window.innerWidth, vh = window.innerHeight;

  function cssPath(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + el.id;
    const cls = (el.className && typeof el.className === "string")
      ? "." + el.className.trim().split(/\s+/).slice(0, 2).join(".") : "";
    const tag = el.tagName.toLowerCase();
    const parent = el.parentElement;
    if (!parent) return tag + cls;
    const idx = Array.from(parent.children).indexOf(el) + 1;
    return `${tag}${cls}:nth-child(${idx})`;
  }

  // 1. 文字截断：只查叶子元素，避免把容器也算进来
  const leafSel = "h1,h2,h3,h4,p,span,a,li,button,td,th,label,div";
  document.querySelectorAll(leafSel).forEach((el) => {
    if (el.children.length > 0) return;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    const txt = (el.textContent || "").trim();
    if (!txt) return;
    const hidden = cs.overflow === "hidden" || cs.overflowX === "hidden"
                 || cs.textOverflow === "ellipsis";
    if (hidden && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 2) {
      out.push({type: "文字截断", level: "error", sel: cssPath(el),
        detail: `"${txt.slice(0, 26)}" 内容宽 ${el.scrollWidth}px，可见 ${el.clientWidth}px（被切掉 ${el.scrollWidth - el.clientWidth}px）`});
    }
  });

  // 2. 页面横向溢出
  const de = document.documentElement;
  if (de.scrollWidth > vw + 2) {
    out.push({type: "页面横向溢出", level: "error", sel: "html",
      detail: `内容总宽 ${de.scrollWidth}px > 视口 ${vw}px，右侧会出现横向滚动条`});
  }

  // 3. 图片：没加载 / 比例变形
  document.querySelectorAll("img").forEach((el) => {
    if (!el.complete || el.naturalWidth === 0) {
      out.push({type: "图片没加载", level: "error", sel: cssPath(el),
        detail: (el.getAttribute("src") || "").slice(0, 70)});
      return;
    }
    const r = el.getBoundingClientRect();
    if (r.width > 4 && r.height > 4 && el.naturalHeight > 0) {
      const nat = el.naturalWidth / el.naturalHeight;
      const disp = r.width / r.height;
      const dev = Math.abs(nat - disp) / nat;
      if (dev > 0.15) {
        out.push({type: "图片比例变形", level: "warn", sel: cssPath(el),
          detail: `原图比例 ${nat.toFixed(2)} → 显示 ${disp.toFixed(2)}（拉伸 ${(dev * 100).toFixed(0)}%）`});
      }
    }
  });

  // 4. 点击区偏小（移动端 44px 是常见下限）
  document.querySelectorAll("button,a,input[type=button],input[type=submit],[role=button]").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    if (r.width < 44 || r.height < 44) {
      const label = (el.textContent || el.getAttribute("aria-label") || "").trim().slice(0, 16);
      out.push({type: "点击区偏小", level: "warn", sel: cssPath(el),
        detail: `"${label}" ${Math.round(r.width)}×${Math.round(r.height)}px，建议 ≥44×44`});
    }
  });

  // 5. 固定元素遮挡
  document.querySelectorAll("header,nav,footer,aside,div").forEach((el) => {
    const cs = getComputedStyle(el);
    if (cs.position !== "fixed" && cs.position !== "sticky") return;
    const r = el.getBoundingClientRect();
    if (r.height > vh * 0.5) {
      out.push({type: "固定元素占屏过多", level: "warn", sel: cssPath(el),
        detail: `高 ${Math.round(r.height)}px，占视口 ${Math.round(r.height / vh * 100)}%，可能压住正文`});
    }
  });

  // 6. 文字重叠：取前 120 个"有独立文字"的元素，两两查相交（O(n²) 但 n 小）
  const txtEls = [];
  document.querySelectorAll("h1,h2,h3,p,li,button,span").forEach((el) => {
    if (txtEls.length >= 120) return;
    const t = (el.textContent || "").trim();
    if (!t || el.children.length > 2) return;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.position === "fixed") return;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return;
    txtEls.push([el, r, cs.position]);
  });
  for (let i = 0; i < txtEls.length; i++) {
    for (let j = i + 1; j < txtEls.length; j++) {
      const [a, ra] = txtEls[i], [b, rb] = txtEls[j];
      if (a.contains(b) || b.contains(a)) continue;
      const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
      const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (ox > 6 && oy > 6) {
        const area = ox * oy;
        if (area > 400) {
          out.push({type: "文字重叠", level: "error", sel: cssPath(b),
            detail: `与 "${(a.textContent || "").trim().slice(0, 14)}" 重叠约 ${Math.round(area)}px²`});
        }
      }
      if (out.length > 60) break;
    }
    if (out.length > 60) break;
  }

  // 7. 对比度（WCAG 相对亮度比，只查纯色背景）
  function lum(rgb) {
    const a = rgb.map((v) => {
      v /= 255;
      return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2];
  }
  function parseColor(s) {
    const m = (s || "").match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(",").map((x) => parseFloat(x.trim()));
    if (p.length === 4 && p[3] < 0.5) return null;      // 半透明跳过
    return p.slice(0, 3);
  }
  const contrastChecked = [];
  document.querySelectorAll("h1,h2,h3,p,span,a,li,button,label").forEach((el) => {
    if (contrastChecked.length >= 80) return;
    if (el.children.length > 0) return;
    const t = (el.textContent || "").trim();
    if (!t) return;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") return;
    const fg = parseColor(cs.color);
    if (!fg) return;
    // 往上找第一个不透明背景
    let bgEl = el, bg = null, depth = 0;
    while (bgEl && depth < 6) {
      const b = parseColor(getComputedStyle(bgEl).backgroundColor);
      if (b) { bg = b; break; }
      bgEl = bgEl.parentElement; depth++;
    }
    if (!bg) bg = [255, 255, 255];
    const l1 = lum(fg), l2 = lum(bg);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    contrastChecked.push(1);
    if (ratio < 3) {
      out.push({type: "对比度不足", level: "error", sel: cssPath(el),
        detail: `"${t.slice(0, 16)}" 对比度 ${ratio.toFixed(2)}:1（建议 ≥4.5:1）`});
    } else if (ratio < 4.5) {
      out.push({type: "对比度偏低", level: "warn", sel: cssPath(el),
        detail: `"${t.slice(0, 16)}" 对比度 ${ratio.toFixed(2)}:1（正文建议 ≥4.5:1）`});
    }
  });

  return out.slice(0, 60);
}
"""


def shot(path: Path, out_dir: Path, selector: str = "", width: int = 1440,
         full_page: bool = True) -> dict:
    """截图。selector 非空时只截那个元素。"""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"shot_{int(__import__('time').time())}.png"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900},
                                device_scale_factor=2)
        page.goto(path.as_uri(), wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(500)
        if selector:
            el = page.query_selector(selector)
            if not el:
                browser.close()
                return {"ok": False, "error": f"没找到元素：{selector}"}
            el.screenshot(path=str(target))
        else:
            page.screenshot(path=str(target), full_page=full_page)
        browser.close()

    return {"ok": True, "file": str(target), "width": width}


def check(path: Path, width: int = 1440) -> dict:
    """加载页面并检测布局问题。"""
    from playwright.sync_api import sync_playwright

    console_errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text[:160])
                if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)[:160]))

        resp = page.goto(path.as_uri(), wait_until="networkidle", timeout=20000)
        page.wait_for_timeout(600)

        try:
            issues = page.evaluate(_JS_CHECK)
        except Exception as e:                            # noqa: BLE001
            issues = [{"type": "检测脚本出错", "level": "error",
                       "detail": str(e)[:200], "sel": ""}]

        # 资源加载失败
        failed = page.evaluate("""() =>
          performance.getEntriesByType('resource')
            .filter(r => r.transferSize === 0 && r.duration > 0 && !r.name.startsWith('data:'))
            .map(r => r.name.split('/').pop()).slice(0, 8)
        """)

        title = page.title()
        browser.close()

    for msg in console_errors[:8]:
        issues.append({"type": "脚本报错", "level": "error", "sel": "",
                       "detail": msg})
    for f in failed or []:
        issues.append({"type": "资源未加载", "level": "warn", "sel": "",
                       "detail": f})

    return {"width": width, "title": title, "issues": issues[:80]}
