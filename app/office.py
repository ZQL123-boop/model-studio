"""Office 文档能力：Word / Excel / PPT / PDF。

## 为什么单独一个模块

tools.py 已经 1200 行，再塞四个文档格式会失控。而且 Office 逻辑只依赖
python-docx / openpyxl / python-pptx / pymupdf，和文件工具没有交集。

## 设计取舍

**一个统一入口 `office(action, path, spec)`，不是四个独立工具。**
原因：工具 schema 已经占 6500 字符，再拆四个每个都要 300+，token 直接爆。
统一入口用 action 分发，模型只需要记一个工具名 + 几个 action 值。

**spec 用 JSON 字符串传，不是嵌套 object。**
OpenAI 的 function schema 对深层嵌套支持一般，30B 填嵌套 JSON 容易漏字段。
JSON 字符串让模型直接写一段 JSON，解析失败时还能把原文返回去看哪错了。

## 各格式的 spec 结构

```python
# Word
{"title": "报告标题", "sections": [
    {"heading": "一、背景", "paras": ["第一段", "第二段"]},
    {"heading": "二、数据", "table": {"header": ["列1","列2"], "rows": [["a","b"]]}},
]}

# Excel
{"sheets": [
    {"name": "销售", "header": ["日期","金额"],
     "rows": [["2026-10-01", 100], ["2026-10-02", 200]],
     "formulas": {"B4": "=SUM(B2:B3)"}},
]}

# PPT
{"slides": [
    {"title": "封面标题", "subtitle": "副标题"},
    {"title": "要点", "bullets": ["第一条", "第二条"]},
]}
```
"""
from __future__ import annotations

import json
from pathlib import Path


def _bad(msg: str) -> str:
    return f"[错误] {msg}"


def _load_spec(spec: str | dict) -> dict | None:
    if isinstance(spec, dict):
        return spec
    try:
        d = json.loads(spec)
    except (ValueError, TypeError) as e:
        return None if not e else None
    return d if isinstance(d, dict) else None


# ---------------------------------------------------------------- Word
def md_to_docx(md_path: Path) -> str:
    """Markdown 文件 → 同名 .docx（放在 md 旁边）。

    为什么要单独一条：让模型自己 read_file 再拼 make_docx 的 spec，
    长文档必然超上下文——它会反复调同一个 office 调用直到被判"卡住"
    （用户实测就是这个报错）。转换交给代码，模型只说一句"转一下"。

    支持：# 标题（1~6 级）、- / * 无序、1. 有序、``` 代码块、| 表格 |、
    普通段落、> 引用。
    """
    import re

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, RGBColor

    src = Path(md_path).expanduser()
    if not src.exists():
        return _bad(f"文件不存在：{src}")
    if src.suffix.lower() not in {".md", ".markdown", ".txt"}:
        return _bad("只支持 .md / .markdown / .txt")

    out = src.with_suffix(".docx")
    try:
        text = src.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _bad(f"读不了源文件：{e}")

    doc = Document()
    lines = text.splitlines()
    i, n = 0, len(lines)
    code_buf: list[str] = []
    stats = {"head": 0, "para": 0, "bullet": 0, "code": 0, "table": 0}

    def flush_code() -> None:
        if not code_buf:
            return
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(8)
        run = p.add_run("\n".join(code_buf))
        run.font.name = "Menlo"
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x44, 0x4A, 0x55)
        code_buf.clear()
        stats["code"] += 1

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # ``` 代码块
        if stripped.startswith("```"):
            if code_buf:
                flush_code()
                i += 1
                continue
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code_buf.append(lines[i])
                i += 1
            flush_code()
            i += 1
            continue

        # 表格：连续的 | 行
        if stripped.startswith("|") and stripped.endswith("|"):
            block = []
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = [[c.strip() for c in r.strip("|").split("|")] for r in block]
            # 去掉 |---|---| 分隔行
            rows = [r for r in rows if not all(set(c) <= set("-: ") and c for c in r)]
            if rows:
                cols = max(len(r) for r in rows)
                t = doc.add_table(rows=0, cols=cols)
                t.style = "Table Grid"
                for ri, r in enumerate(rows):
                    cells = t.add_row().cells
                    for ci in range(cols):
                        cells[ci].text = r[ci] if ci < len(r) else ""
                        if ri == 0:
                            for para in cells[ci].paragraphs:
                                for run in para.runs:
                                    run.bold = True
                stats["table"] += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 4) - 1
            doc.add_heading(m.group(2).strip(), level=level)
            stats["head"] += 1
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            p = doc.add_paragraph(stripped.lstrip("> ").strip())
            p.paragraph_format.left_indent = Pt(18)
            for run in p.runs:
                run.italic = True
                run.font.color.rgb = RGBColor(0x66, 0x6E, 0x7B)
            stats["para"] += 1
            i += 1
            continue

        # 列表
        if re.match(r"^[-*+]\s+", stripped):
            doc.add_paragraph(re.sub(r"^[-*+]\s+", "", stripped), style="List Bullet")
            stats["bullet"] += 1
            i += 1
            continue
        if re.match(r"^\d+[.)]\s+", stripped):
            doc.add_paragraph(re.sub(r"^\d+[.)]\s+", "", stripped), style="List Number")
            stats["bullet"] += 1
            i += 1
            continue

        # 水平线
        if re.fullmatch(r"[-*_]{3,}", stripped):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run("—" * 20)
            run.font.color.rgb = RGBColor(0xBB, 0xBB, 0xBB)
            i += 1
            continue

        if stripped:
            # 行内 **粗体** / `代码` 简单处理
            p = doc.add_paragraph()
            for k, seg in enumerate(re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", stripped)):
                if seg.startswith("**") and seg.endswith("**") and len(seg) > 4:
                    r = p.add_run(seg[2:-2])
                    r.bold = True
                elif seg.startswith("`") and seg.endswith("`") and len(seg) > 2:
                    r = p.add_run(seg[1:-1])
                    r.font.name = "Menlo"
                    r.font.size = Pt(10)
                elif seg:
                    p.add_run(seg)
            stats["para"] += 1
        i += 1

    flush_code()
    try:
        doc.save(str(out))
    except OSError as e:
        return _bad(f"保存失败：{e}")

    size_kb = out.stat().st_size / 1024
    return (f"[已转换] {out}（{size_kb:.0f} KB）\n"
            f"标题 {stats['head']} 个、段落 {stats['para']} 段、"
            f"列表 {stats['bullet']} 条、代码块 {stats['code']} 个、表格 {stats['table']} 个\n"
            f"源文件：{src.name}")


def make_docx(path: Path, spec: dict) -> str:
    from docx import Document
    from docx.shared import Pt

    doc = Document()

    title = (spec.get("title") or "").strip()
    if title:
        h = doc.add_heading(title, level=0)
        for run in h.runs:
            run.font.size = Pt(22)

    for sec in spec.get("sections") or []:
        head = (sec.get("heading") or "").strip()
        if head:
            doc.add_heading(head, level=int(sec.get("level") or 1))

        for para in sec.get("paras") or []:
            text = str(para).strip()
            if text:
                doc.add_paragraph(text)

        for item in sec.get("bullets") or []:
            doc.add_paragraph(str(item), style="List Bullet")

        tb = sec.get("table") or {}
        header = tb.get("header") or []
        rows = tb.get("rows") or []
        if header:
            t = doc.add_table(rows=1, cols=len(header))
            t.style = "Light Grid Accent 1"
            for i, c in enumerate(header):
                t.rows[0].cells[i].text = str(c)
            for r in rows:
                cells = t.add_row().cells
                for i, c in enumerate(r[: len(header)]):
                    cells[i].text = str(c)

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    n_par = len(doc.paragraphs)
    n_tb = len(doc.tables)
    return f"[已生成] {path}\n段落 {n_par} 个，表格 {n_tb} 个"


def read_docx(path: Path, max_chars: int = 4000) -> str:
    from docx import Document

    doc = Document(str(path))
    out = [f"【Word】{path.name}"]
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            style = p.style.name if p.style else ""
            prefix = "#" if "Heading" in style else ""
            out.append(f"{prefix} {t}")
    for i, tb in enumerate(doc.tables, 1):
        out.append(f"\n[表格 {i}]")
        for row in tb.rows:
            out.append(" | ".join(c.text.strip() for c in row.cells))
    text = "\n".join(out)
    return text[:max_chars] + ("\n…（截断）" if len(text) > max_chars else "")


# ---------------------------------------------------------------- Excel
def make_xlsx(path: Path, spec: dict) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    wb.remove(wb.active)

    sheets = spec.get("sheets") or []
    for sh in sheets:
        name = (sh.get("name") or "Sheet").strip()[:31]
        ws = wb.create_sheet(name)
        header = sh.get("header") or []
        rows = sh.get("rows") or []

        if header:
            ws.append([str(c) for c in header])
            for c in ws[1]:
                c.font = Font(bold=True)
                c.fill = PatternFill("solid", fgColor="EEEEEE")
                c.alignment = Alignment(horizontal="center")

        for r in rows:
            ws.append(list(r))

        # 公式：{"B10": "=SUM(B2:B9)"}
        for addr, f in (sh.get("formulas") or {}).items():
            ws[addr] = str(f)

        # 简单列宽自适应
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(width + 4, 40)

    if not sheets:
        wb.create_sheet("Sheet1")

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return f"[已生成] {path}\n{len(sheets)} 个工作表：{', '.join(s.get('name', '') for s in sheets)}"


def read_xlsx(path: Path, max_rows: int = 30) -> str:
    from openpyxl import load_workbook

    # data_only=True 读公式的**计算结果**（需要 Excel 存过一次才有值）
    wf = load_workbook(str(path), data_only=False)
    wv = load_workbook(str(path), data_only=True)
    out = [f"【Excel】{path.name}  {len(wf.sheetnames)} 个工作表"]

    # 公式没被 Excel 算过（值全是 None）时，用 formulas 库自己算一遍，
    # 否则模型只看到「未计算」，没法核对结果。
    need_calc = False
    for name in wf.sheetnames:
        for row in wf[name].iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("="):
                    if wv[name].cell(c.row, c.column).value is None:
                        need_calc = True
                        break
            if need_calc:
                break
        if need_calc:
            break

    calc_vals: dict[str, str] = {}
    if need_calc:
        try:
            import re as _re

            import formulas

            sol = formulas.ExcelModel().loads(str(path)).finish().calculate()
            for k, val in sol.items():
                m = _re.search(r"!([A-Z]+\d+)$", str(k))
                if not m:
                    continue
                mm = _re.search(r"=\[\[([^\]]+)\]\]", str(val))
                if mm:
                    calc_vals[m.group(1)] = mm.group(1)
        except Exception:                                 # noqa: BLE001
            pass

    for name in wf.sheetnames:
        sf, sv = wf[name], wv[name]
        out.append(f"\n--- {name}（{sf.max_row} 行 × {sf.max_column} 列）---")
        shown = 0
        for r in range(1, min(sf.max_row, max_rows) + 1):
            cells = []
            for c in range(1, min(sf.max_column, 10) + 1):
                f = sf.cell(r, c).value
                v = sv.cell(r, c).value
                if f is None:
                    continue
                if isinstance(f, str) and f.startswith("="):
                    # 公式：显示 公式=结果（优先 Excel 缓存值，其次自己算的）
                    addr = f"{chr(64 + c)}{r}" if c <= 26 else ""
                    vv = v if v is not None else calc_vals.get(addr, "（未计算）")
                    cells.append(f"{f}→{vv}")
                else:
                    cells.append(str(f))
            if cells:
                out.append("  " + " | ".join(cells))
                shown += 1
        if sf.max_row > max_rows:
            out.append(f"  …（共 {sf.max_row} 行，只显示前 {max_rows}）")

    text = "\n".join(out)
    return text[:5000] + ("\n…（截断）" if len(text) > 5000 else "")


# ---------------------------------------------------------------- PPT
def make_pptx(path: Path, spec: dict) -> str:
    from pptx import Presentation
    from pptx.util import Pt

    prs = Presentation()
    slides = spec.get("slides") or []

    for s in slides:
        title = (s.get("title") or "").strip()
        subtitle = (s.get("subtitle") or "").strip()
        bullets = s.get("bullets") or []

        if subtitle or not bullets:
            # 封面页
            layout = prs.slide_layouts[0]
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = title or "未命名"
            if subtitle and len(slide.placeholders) > 1:
                slide.placeholders[1].text = subtitle
        else:
            layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = title or "未命名"
            body = slide.placeholders[1].text_frame
            body.text = str(bullets[0])
            for b in bullets[1:]:
                p = body.add_paragraph()
                p.text = str(b)
                p.font.size = Pt(18)

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    return f"[已生成] {path}\n{len(prs.slides)} 页"


def read_pptx(path: Path) -> str:
    from pptx import Presentation

    prs = Presentation(str(path))
    out = [f"【PPT】{path.name}  {len(prs.slides)} 页"]
    for i, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t.replace("\n", " / "))
        out.append(f"\n第 {i} 页：" + (" ｜ ".join(texts) if texts else "（无文字）"))
    return "\n".join(out)[:4000]


# ---------------------------------------------------------------- PDF
def convert_to_pdf(path: Path) -> str:
    """Office → PDF。需要 LibreOffice（本机没装，返回明确提示）。

    为什么不做"用 python-docx 直接转 PDF"：docx 的排版（样式、分页、页眉页脚）
    只有真正的排版引擎能还原。python-docx 读的是 XML 结构，不是渲染结果。
    """
    import shutil
    import subprocess

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        for cand in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",):
            if Path(cand).exists():
                soffice = cand
                break
    if not soffice:
        return ("[需要 LibreOffice] 本机没装，Office 转 PDF 需要它。\n"
                "安装：brew install --cask libreoffice（约 700 MB）\n"
                "或者：直接用 make_docx/make_xlsx 生成原文件，让用户自己导出。")

    outdir = path.parent
    try:
        r = subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(path)],
            capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "[错误] 转换超时（>180 秒）"
    pdf = outdir / (path.stem + ".pdf")
    if pdf.exists():
        return f"[已转换] {pdf}（{pdf.stat().st_size / 1024:.0f} KB）"
    return f"[转换失败] {r.stderr[:200]}"


def render_pdf(path: Path, pages: str = "1", dpi: int = 110) -> str:
    """PDF 页面渲染成 PNG，用于检查排版。

    pages 支持 "1" / "1,3" / "1-3" / "all"。
    """
    import pymupdf

    doc = pymupdf.open(str(path))
    total = doc.page_count

    want: list[int] = []
    p = (pages or "1").strip().lower()
    if p == "all":
        want = list(range(total))
    else:
        for part in p.split(","):
            part = part.strip()
            if "-" in part:
                a, _, b = part.partition("-")
                try:
                    want.extend(range(int(a) - 1, int(b)))
                except ValueError:
                    pass
            elif part.isdigit():
                want.append(int(part) - 1)
    want = [i for i in want if 0 <= i < total][:8]     # 一次最多 8 页
    if not want:
        return f"[错误] 页码不对（共 {total} 页），用 1 / 1,3 / 1-3 / all"

    outdir = path.parent / (path.stem + "_pages")
    outdir.mkdir(parents=True, exist_ok=True)
    saved = []
    for i in want:
        pg = doc.load_page(i)
        pix = pg.get_pixmap(dpi=dpi)
        f = outdir / f"page_{i + 1}.png"
        pix.save(str(f))
        saved.append(f)

    doc.close()
    rel = [f"/generated/{f.name}" for f in saved]     # 如果放在 generated 下可直接看
    return (f"[已渲染] 共 {total} 页，导出了第 {', '.join(str(i + 1) for i in want)} 页\n"
            + "\n".join(f"  {f}" for f in saved)
            + "\n\n检查排版时用 read_file 描述图片内容，或直接让用户看这几张图。")


# ---------------------------------------------------------------- 统一入口
def run(action: str, path: str, spec: str = "", **kw) -> str:
    """分发。path 是绝对路径（由 tools 层校验过写权限）。"""
    act = (action or "").strip().lower()
    p = Path(path).expanduser()
    if not str(p).startswith("/"):
        return _bad("path 必须是绝对路径或以 ~ 开头")

    if act == "make_docx":
        d = _load_spec(spec)
        if d is None:
            return _bad("spec 不是合法 JSON。示例：{\"title\":\"标题\",\"sections\":[{\"heading\":\"一\",\"paras\":[\"内容\"]}]}")
        return make_docx(p, d)

    if act == "read_docx":
        return read_docx(p) if p.exists() else _bad(f"文件不存在：{p}")

    if act == "make_xlsx":
        d = _load_spec(spec)
        if d is None:
            return _bad("spec 不是合法 JSON。示例：{\"sheets\":[{\"name\":\"表1\",\"header\":[\"A\"],\"rows\":[[1]]}]}")
        return make_xlsx(p, d)

    if act == "read_xlsx":
        return read_xlsx(p) if p.exists() else _bad(f"文件不存在：{p}")

    if act == "make_pptx":
        d = _load_spec(spec)
        if d is None:
            return _bad("spec 不是合法 JSON。示例：{\"slides\":[{\"title\":\"封面\",\"subtitle\":\"副标题\"},{\"title\":\"要点\",\"bullets\":[\"一\",\"二\"]}]}")
        return make_pptx(p, d)

    if act == "read_pptx":
        return read_pptx(p) if p.exists() else _bad(f"文件不存在：{p}")

    if act == "md2docx":
        return md_to_docx(p)

    if act == "to_pdf":
        return convert_to_pdf(p) if p.exists() else _bad(f"文件不存在：{p}")

    if act == "render_pdf":
        if not p.exists():
            return _bad(f"文件不存在：{p}")
        return render_pdf(p, pages=str(kw.get("pages") or "1"))

    return _bad(f"不认识的 action：{act}。支持 make_docx / read_docx / make_xlsx / "
                "read_xlsx / make_pptx / read_pptx / to_pdf / render_pdf")
