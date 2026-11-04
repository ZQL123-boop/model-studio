"""图像理解：OCR + 版面结构（macOS 自带 Vision 框架）。

## 为什么用 Vision 而不是 tesseract

- 系统自带，不用装额外二进制（tesseract 要 brew + 中文语言包）
- 中文识别质量明显好于 tesseract
- 离线、免费、Apple 芯片加速
- 直接给出每个文本框的坐标，能重建版面

## 局限

只能读**文字**。图片里画的是什么（一只猫、一个折线趋势）它不知道——
那需要视觉语言模型（VL），本机 30B 是纯文本模型，跑不了。
所以这个模块的定位是：**把图里的文字和结构读出来，交给文本模型推理**。
对截图、扫描件、表格、带标签的图表够用；纯照片/无文字海报读不出内容。
"""
from __future__ import annotations

from pathlib import Path


def _load_image(path: Path):
    import Quartz

    url = Quartz.CFURLCreateFromFileSystemRepresentation(
        None, str(path).encode("utf-8"), len(str(path).encode("utf-8")), False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    if src is None:
        return None
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def ocr(path: Path, lang: str = "zh-Hans,en-US", accurate: bool = True) -> dict:
    """OCR 一张图，返回按阅读顺序排好的文本行（含位置和置信度）。"""
    import Vision

    img = _load_image(path)
    if img is None:
        return {"ok": False, "error": "图片读不了（不是有效图像或格式不支持）"}

    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(
        Vision.VNRequestTextRecognitionLevelAccurate if accurate
        else Vision.VNRequestTextRecognitionLevelFast
    )
    req.setRecognitionLanguages_([x.strip() for x in lang.split(",") if x.strip()])
    req.setUsesLanguageCorrection_(True)

    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        return {"ok": False, "error": f"OCR 失败：{err}"}

    lines = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        c = cands[0]
        bb = obs.boundingBox()          # 归一化坐标，(0,0) 在左下角
        lines.append({
            "text": c.string(),
            "conf": round(float(c.confidence()), 3),
            "x": round(float(bb.origin.x), 4),
            "y": round(float(bb.origin.y), 4),
            "w": round(float(bb.size.width), 4),
            "h": round(float(bb.size.height), 4),
        })

    # 按阅读顺序：从上到下（y 大在上），同一行从左到右
    lines.sort(key=lambda l: (-round(l["y"], 2), l["x"]))

    # 重建"行"：y 接近的合并成一行（容纳同一行里被拆开的多个文本块）
    rows: list[dict] = []
    for ln in lines:
        placed = False
        for row in rows:
            if abs(row["y"] - ln["y"]) < max(0.012, ln["h"] * 0.6):
                row["parts"].append(ln)
                placed = True
                break
        if not placed:
            rows.append({"y": ln["y"], "parts": [ln]})
    for row in rows:
        row["parts"].sort(key=lambda l: l["x"])

    return {
        "ok": True,
        "count": len(lines),
        "rows": [
            {"y": round(r["y"], 3),
             "text": "  ".join(p["text"] for p in r["parts"])}
            for r in rows
        ],
        "lines": lines,
    }


def describe_with_ollama(path: Path, question: str = "详细描述这张图的内容",
                          model: str = "qwen2.5vl:3b") -> str | None:
    """用 Ollama 的视觉模型理解画面（OCR 做不到的部分：图里画的是什么）。

    返回 None 表示视觉模型没装/没跑，调用方跳过即可。
    """
    import base64

    import httpx

    try:
        b64 = base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return None
    try:
        with httpx.Client(trust_env=False, timeout=120) as c:
            r = c.post("http://127.0.0.1:11434/api/generate",
                       json={"model": model, "prompt": question,
                             "images": [b64], "stream": False,
                             # 用完后 1 分钟自动卸载，释放内存给 30B。
                             # 连续看图时不重复加载，闲置就关。
                             "keep_alive": "1m"})
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    try:
        resp = (r.json().get("response") or "").strip()
    except ValueError:
        return None
    return resp if resp else None


def read_image(path: Path, max_chars: int = 6000) -> str:
    """给模型看的格式化结果：先整体文本，再带坐标的行（用于判断版面）。"""
    r = ocr(path)
    if not r.get("ok"):
        return f"[错误] {r.get('error')}"

    if r["count"] == 0:
        # 没文字也试视觉模型——照片/插画就是靠它
        desc = describe_with_ollama(path)
        if desc:
            return "[没有文字，画面内容如下]\n" + desc
        return ("[没有识别到文字] 这张图里没有可读文本，视觉模型也没返回内容"
                "（可能没装或没启动）。")

    out = [f"【识别到 {r['count']} 个文本块，{len(r['rows'])} 行】", "", "== 文字（OCR）=="]
    total = 0
    for row in r["rows"]:
        t = row["text"].strip()
        if not t:
            continue
        out.append(t)
        total += len(t)
        if total > max_chars:
            out.append("…（文字太多，已截断）")
            break

    # 低置信度的挑出来提醒
    low = [l for l in r["lines"] if l["conf"] < 0.5]
    if low:
        out.append("")
        out.append(f"== 可能识别错的 {len(low)} 处（置信度 < 0.5）==")
        for l in low[:8]:
            out.append(f"  「{l['text']}」 conf={l['conf']}")

    # 版面提示：如果行高差异大，说明有标题/正文层级
    heights = sorted({round(l["h"], 3) for l in r["lines"]}, reverse=True)
    if len(heights) >= 2 and heights[0] > heights[-1] * 1.6:
        out.append("")
        out.append(f"【版面】文字大小差异明显（最大 {heights[0]:.3f} vs 最小 {heights[-1]:.3f}），"
                   "大概率有标题层级。")

    # 再试视觉模型描述画面（OCR 只拿文字，画面内容靠 VL）
    desc = describe_with_ollama(path)
    if desc:
        out.append("")
        out.append("== 画面内容（视觉模型）==")
        out.append(desc)

    return "\n".join(out)
