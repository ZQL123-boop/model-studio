"""路由器推理：一句话 → 工具名 + 参数。

分工：
    小模型（LoRA 分类器） → 只选工具（18 选 1），输出 3~20 token
    规则                  → 能抽到的参数就地抽（路径/URL/命令/城市/币种）
    30B                   → 规则抽不到的才交给它（在调用方决定）

## 为什么参数不交给小模型

实测（v2）：web_search 选对 100% 但 query 只对 4.2%，
knowledge_search 选对 40.5%、query 0%。小模型会把用户原话原样塞进 query，
也会给不存在的路径。这类错误"看起来能跑"，实际全是废调用。

规则能覆盖的参数有个共同点：**它们在文本里是显式出现的**（路径、URL、命令、城市），
正则抓比模型稳得多。真正需要理解的（画图数据、工具 action 选择）才交给大模型。

用法：
    from router import Router
    r = Router()                       # 默认 0.5B + training/adapters_cls
    r.route("把 ~/Desktop/笔记.md 转成 docx")
    # {'tool': 'office', 'args': {'action': 'md2docx', 'path': '~/Desktop/笔记.md'},
    #  'need_llm_args': False}

命令行：
    python training/router.py "读一下 ~/Desktop/笔记.md"
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJ))

TOOLS = ["read_file", "write_file", "edit_file", "list_dir", "find_files", "grep_code",
         "run_bash", "web_search", "fetch_url", "knowledge_search", "get_weather",
         "get_exchange_rate", "generate_image", "make_chart", "analyze_data",
         "office", "design", "set_goal"]

CITIES = ["深圳", "中山", "北京", "上海", "广州", "成都", "杭州", "武汉", "西安",
          "南京", "珠海", "东莞", "天津", "重庆", "苏州", "厦门", "长沙", "青岛"]

CURRENCY_ALIAS = {
    "美元": "USD", "美金": "USD", "日元": "JPY", "欧元": "EUR", "港币": "HKD",
    "港元": "HKD", "英镑": "GBP", "澳元": "AUD", "加元": "CAD", "新加坡元": "SGD",
    "韩元": "KRW", "泰铢": "THB", "新台币": "TWD", "瑞士法郎": "CHF",
}

_PATH_RE = re.compile(r"(~/[^\s，,。；;）)]+|/[A-Za-z0-9_./\-]{3,})")
_URL_RE = re.compile(r"https?://[^\s，,。；;）)]+")
_QUOTE_RE = re.compile(r"[「“\"']([^」”\"']+)[」”\"']")


# --------------------------------------------------------------------- 参数抽取
def _path(text: str) -> str | None:
    m = _PATH_RE.search(text)
    if m:
        return m.group(1).rstrip(".")
    # 只给了文件名（如 app/main.py）
    m = re.search(r"([A-Za-z0-9_\-]+/[A-Za-z0-9_./\-]+\.[A-Za-z0-9]{1,6})", text)
    if m:
        return m.group(1)
    m = re.search(r"([A-Za-z0-9_\-]+\.(?:md|py|json|txt|csv|xlsx|yaml|html|log|sh))", text)
    return m.group(1) if m else None


def _clean_query(text: str) -> str:
    q = re.sub(r"^(帮我|帮忙|请|麻烦)?\s*(搜一下|搜搜|搜索|查一下|查查|看看|查|找一下)\s*", "", text)
    # 别把"最新版本"这种关键限定词也剥掉（"FastAPI 最新版本" 才是好检索词）
    q = re.sub(r"(现在|今天|最近)?(是多少|是几|怎么样|怎么弄|怎么办|吗|呢)?[？?。！!]*$", "", q)
    # 末尾的动作词也去掉（"最近显卡价格怎么样，搜一下" → "最近显卡价格"）
    q = re.sub(r"[，,\s]*(搜一下|搜索一下|查一下|查查|搜搜|看看|说一下)$", "", q)
    return q.strip(" ，,。?？") or text.strip()


def extract_args(tool: str, text: str) -> tuple[dict, bool]:
    """返回 (参数, 是否需要 30B 补)。规则抽不全就置 True。"""
    t = text.strip()

    if tool == "fetch_url":
        m = _URL_RE.search(t)
        return ({"url": m.group(0)} if m else {}), not bool(m)

    if tool in {"read_file", "list_dir", "analyze_data"}:
        p = _path(t)
        return ({"path": p} if p else {}), p is None

    if tool == "write_file":
        p = _path(t)
        return ({"path": p, "content": ""} if p else {}), p is None

    if tool == "edit_file":
        p = _path(t)
        quoted = _QUOTE_RE.findall(t)
        if p and len(quoted) >= 2:
            return {"path": p, "old_string": quoted[0], "new_string": quoted[1]}, False
        return ({"path": p} if p else {}), True

    if tool == "find_files":
        m = re.search(r"(?:后缀|扩展名)?\s*[「“\"]?(\w{1,8})[」”\"]?\s*(?:后缀|扩展名|文件)", t)
        if m:
            return {"pattern": f"*.{m.group(1)}"}, False
        return {}, True

    if tool == "grep_code":
        quoted = _QUOTE_RE.findall(t)
        if quoted:
            return {"pattern": quoted[0]}, False
        m = re.search(r"(?:用了|提到|包含|搜)\s*([A-Za-z0-9_().\[\]{}:;=+*/\\-]{2,})", t)
        return ({"pattern": m.group(1)} if m else {}), not bool(m)

    if tool == "run_bash":
        quoted = _QUOTE_RE.findall(t)
        if quoted:
            return {"command": quoted[0]}, False
        m = re.search(r"(?:跑一下|执行|运行|跑)\s*(.+)$", t)
        if m:
            cmd = m.group(1).strip(" 的，,。")
            # 命令一般是纯 ASCII，后面跟着"看看磁盘"这类中文说明要切掉
            mm = re.match(r"([\x20-\x7e]+)", cmd)
            if mm:
                cmd = mm.group(1).strip()
            if cmd:
                return {"command": cmd}, False
        return {}, True

    if tool == "web_search":
        return {"query": _clean_query(t)}, False

    if tool == "knowledge_search":
        q = re.sub(r"(的原理是什么|怎么理解|讲讲|查一下知识库里的|本地资料里有没有|知识库搜)", "", t)
        return {"query": q.strip(" ，,。?？") or t}, False

    if tool == "get_weather":
        for c in CITIES:
            if c in t:
                return {"city": c}, False
        return {}, True

    if tool == "get_exchange_rate":
        for alias, code in CURRENCY_ALIAS.items():
            if alias in t:
                return {"base": code, "targets": "CNY"}, False
        m = re.search(r"\b([A-Z]{3})\b", t)
        return ({"base": m.group(1), "targets": "CNY"} if m else {}), not bool(m)

    if tool == "generate_image":
        q = re.sub(r"^(画|生成|给我)?(一张|张|个|一个)?", "", t)
        q = re.sub(r"(的图|的插画|图片|图)$", "", q).strip(" ，,。")
        return ({"prompt": q} if q else {}), not bool(q)

    if tool == "office":
        p = _path(t)
        args = {"action": "", "path": p} if p else {"action": ""}
        low = t.lower()
        if "docx" in low or "word" in low:
            args["action"] = "md2docx"
        elif "xlsx" in low or "表格" in t or "excel" in low:
            args["action"] = "read_xlsx"
        elif "pptx" in low or "ppt" in low:
            args["action"] = "make_pptx"
        return args, not (p and args["action"])

    if tool == "design":
        p = _path(t)
        act = "shot" if ("截图" in t or "截个图" in t) else "check"
        return ({"action": act, "path": p} if p else {"action": act}), p is None

    if tool == "set_goal":
        q = re.sub(r"^(记一下我的目标[:：]?|把|添加目标|设成目标|我的目标是)", "", t).strip(" 「」“”\"，,。")
        return ({"goal": q} if q else {}), not bool(q)

    # make_chart 需要具体数值，规则不该瞎猜
    return {}, True


# --------------------------------------------------------------------- 模型推理
class Router:
    def __init__(self, model: str = "models/qwen2.5-0.5b-4bit",
                 adapter: str = "training/adapters_cls05c") -> None:
        from mlx_lm import load

        self.model, self.tok = load(str(PROJ / model),
                                   adapter_path=str(PROJ / adapter) if adapter else None)
        self.adapter = adapter

    def classify(self, question: str) -> tuple[str | None, str]:
        from mlx_lm import generate

        msgs = [{"role": "system", "content": self._system()},
                {"role": "user", "content": question}]
        try:
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True,
                                                  tokenize=False, enable_thinking=False)
        except TypeError:
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True,
                                                  tokenize=False)
        out = generate(self.model, self.tok, prompt=prompt, max_tokens=16, verbose=False)
        return self._normalize(out), out.strip()

    @staticmethod
    def _system() -> str:
        from build_dataset import CATALOG
        return ("你是工具路由器。根据用户请求，从下面选一个最合适的工具，"
                "只输出工具名，不要解释、不要标点：\n"
                + "\n".join(f"{n}：{d}" for n, d in CATALOG))

    @staticmethod
    def _normalize(text: str) -> str | None:
        t = re.sub(r"[\s`'\"。，,：:;；!！?？<>|]+", "", (text or "").lower())
        for name in TOOLS:
            if name in t:
                return name
        return None

    def route(self, question: str) -> dict:
        tool, raw = self.classify(question)
        if not tool:
            return {"tool": None, "args": {}, "need_llm_args": True,
                    "raw": raw, "note": "输出不在工具表里，建议拒答或交 30B"}
        args, need = extract_args(tool, question)
        return {"tool": tool, "args": args, "need_llm_args": need, "raw": raw}


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "把 ~/Desktop/笔记.md 转成 docx"
    r = Router()
    print("问题:", q)
    print("结果:", r.route(q))
