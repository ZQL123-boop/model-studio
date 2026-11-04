"""Agent 主循环：任务状态 + 计划注入 + 无进展检测。

## 解决什么问题

原来的 `/api/agent/stream` 是「调工具 → 看结果 → 再调」的裸循环，
四个轮次用完就停。30B 在这种结构下会绕圈：

    调 list_dir → 没找到 → 换个路径再 list → 还没找到 → 再换 → 轮次用完

三个补丁：

1. **计划注入**：让模型先用 `write_plan` 列出步骤，之后每轮把计划和进度
   重新塞进 system。它随时能看到"我在第几步、还剩几步"，不容易反复试错。
2. **无进展检测**：连续 3 次完全相同的 (tool, args) 就中断。
   模型卡住时不会自己跳出来，得外面拽一把。
3. **轮次提到 12**：4 轮对稍微复杂一点的任务根本不够。

## 不解决什么

规划深度是**模型能力**，不是代码能补的。本地 30B（激活 3.3B）做 4~6 步
任务会吃力，10 步以上基本没戏。这里只让它"不乱绕"，不是让它"变强"。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import guard

MAX_TURNS = 12
STUCK_REPEAT = 3          # 连续几次相同操作算卡住
TOOL_RESULT_LIMIT = 1200


@dataclass
class AgentState:
    goal: str = ""
    plan: list[str] = field(default_factory=list)
    cursor: int = 0                       # 当前做到第几步（0-based）
    log: list[dict] = field(default_factory=list)
    recent: list[str] = field(default_factory=list)   # 最近操作的签名
    fails: int = 0                                    # 连续失败次数
    stuck_hits: int = 0                               # 同一调用重复被警告了几次
    cache: dict = field(default_factory=dict)         # (tool,args) -> 结果，任务内复用

    def system_block(self) -> str:
        """每轮注入的进度块。没有计划时返回空串。"""
        if not self.plan:
            return ""
        lines = ["【当前任务】", self.goal or "（未明确）", "", "【执行计划】"]
        for i, step in enumerate(self.plan):
            if i < self.cursor:
                mark = "[已完成]"
            elif i == self.cursor:
                mark = "[进行中]"
            else:
                mark = "[待办]"
            lines.append(f"{mark} {i + 1}. {step}")
        lines.append("")
        lines.append("需要调整计划、或某一步做完要标记进度时，才调 write_plan。")
        lines.append("**计划没变就不要重复调用**——直接做下一步。")
        return "\n".join(lines)

    def push(self, tool: str, args: dict) -> bool:
        """记录一次操作，返回 True 表示"同一调用又来了"（还不到放弃的程度）。"""
        sig = tool + "|" + json.dumps(args, sort_keys=True, ensure_ascii=False)
        self.recent.append(sig)
        if len(self.recent) < STUCK_REPEAT:
            return False
        tail = self.recent[-STUCK_REPEAT:]
        return len(set(tail)) == 1


PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "write_plan",
        "description": (
            "开始任务前先列步骤，之后每完成一步更新 current。"
            "简单的一步任务可以跳过不调。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "步骤列表，每项一句话，最多 6 步",
                },
                "current": {
                    "type": "integer",
                    "description": "当前进行到第几步（从 0 开始）。列计划时填 0。",
                },
            },
            "required": ["steps"],
        },
    },
}


def all_tools(base_tools: list[dict]) -> list[dict]:
    return [PLAN_TOOL] + base_tools


def looks_like_raw_payload(text: str) -> bool:
    """回答是不是"裸数据"（JSON/XML/HTML 代码块）而不是自然语言。

    实测：问"查一下中山天气"，模型（尤其温度高时）不调工具，
    直接甩一段 JSON/XML —— 内容还是编的（16℃、湿度 70%）。
    这类回答必须打回重试，否则用户拿到的是一坨假数据。
    """
    t = (text or "").strip()
    if not t or len(t) > 1500:
        return False
    head = t[:200].lstrip()
    if head.startswith("```"):
        head = head.split("\n", 1)[-1].lstrip()
    low = head.lower()
    if low.startswith(("<?xml", "<html", "<!doctype", "{\"", "{\n", "[{")):
        return True
    if head[:1] in "{[" and ("\"" in head or ":" in head):
        cjk = sum(1 for ch in head if "\u4e00" <= ch <= "\u9fff")
        return cjk < len(head) * 0.15
    return False


REFUSAL_WORDS = (
    "无法", "不能", "做不到", "没有权限", "无权限", "无法获取", "无法访问",
    "无法联网", "不能联网", "请你", "你可以自己", "你可以访问", "请自行",
    "我没有", "我无法", "没法", "办不到",
)

# 用户意图明显指向「有工具能干」的实时/动作类活。用于判断"拒绝"是不是真该拒绝。
TOOL_INTENT_WORDS = (
    "天气", "温度", "几度", "汇率", "价格", "多少钱", "最新", "今天", "现在",
    "当前", "实时", "新闻", "行情", "股价", "搜索", "查", "读", "写", "运行",
    "执行", "文件", "代码", "画", "生成", "下载",
)


def looks_like_refusal(answer: str, question: str) -> bool:
    """模型该调工具却直接拒绝（"我无法获取实时数据 / 做不到 / 没权限"）。

    只看回答文本里的拒绝词不够——「你能飞吗 → 我不能飞」是对的。
    必须同时满足：用户的问题指向一个明显有工具能干的活。
    """
    a = (answer or "").lower()
    q = (question or "").lower()
    if not any(w in a for w in REFUSAL_WORDS):
        return False
    return any(w in q for w in TOOL_INTENT_WORDS)


def looks_like_tool_intent(question: str) -> bool:
    """问题是否疑似指向「有工具能干」的活。用于决定要不要调路由器小模型
    （纯寒暄/知识问答跳过，省 12 秒模型加载）。"""
    q = (question or "").lower()
    return any(w in q for w in TOOL_INTENT_WORDS)


def record(state: AgentState, name: str, args: dict, result: str) -> None:
    """并行执行完后补记 state（log + cache）。并行时不能在里面改 state。"""
    state.log.append({"tool": name, "args": args,
                      "preview": result.replace("\n", " ")[:160]})
    if name in READ_ONLY_TOOLS:
        import json as _json
        key = name + "|" + _json.dumps(args, sort_keys=True, ensure_ascii=False)
        state.cache[key] = result


def execute(state: AgentState, name: str, args: dict, tools_mod) -> tuple[str, bool]:
    """执行一次工具调用，返回 (结果文本, 是否卡住)。"""
    if name == "write_plan":
        steps = args.get("steps") or []
        if not isinstance(steps, list):
            return "[错误] steps 必须是数组", False
        new_plan = [str(x)[:120] for x in steps][:6]
        try:
            new_cursor = max(0, min(int(args.get("current") or 0), len(new_plan)))
        except (TypeError, ValueError):
            new_cursor = 0
        # 只接受"进度前进"。模型每轮重新生成的步骤文字常有细微差别，
        # 用文本相等判断拦不住——实测同一个任务被调了 6 次 write_plan。
        # 按 cursor 判断：没前进就是重复。
        if state.plan and new_cursor <= state.cursor:
            return ("[计划已存在，不要重复列计划] 直接做当前这一步："
                    f"第 {state.cursor + 1} 步「{state.plan[state.cursor]}」。"
                    "做完再调我、把 current 加一。"), False
        state.plan = new_plan
        state.cursor = new_cursor
        cur = state.plan[state.cursor] if state.plan else "（空）"
        return f"[计划已更新] 共 {len(state.plan)} 步，当前第 {state.cursor + 1} 步：{cur}", False

    stuck = state.push(name, args)

    # 拦"死磕 curl 抓网页"：实测 12 轮全耗在同一个知乎页面上换正则，
    # 最后一句话都没给用户。抓网页有专门的 fetch_url，run_bash 只该用来跑代码。
    if name == "run_bash":
        cmd = str((args or {}).get("command") or "")
        low = cmd.lower()
        if "curl " in low or "wget " in low:
            web_tries = sum(
                1 for r in state.log
                if r.get("tool") == "run_bash"
                and any(k in str((r.get("args") or {}).get("command") or "").lower()
                        for k in ("curl ", "wget ")))
            if web_tries >= 2:
                return ("[拒绝] 已经用 curl/wget 抓过 2 次网页，别再抓了。"
                        "改用 `fetch_url(<一条新 URL>)`；如果换了来源还是拿不到数据，"
                        "就停止抓取，改用已有信息做降级交付（画图 + 在 note 里写明"
                        "数据来源和局限），并把缺口说清楚。"), False

    # 只读工具的结果在同一个任务里缓存：模型常把同一个文件读两遍
    # （先看结构、再读细节时手滑重读全文），第二次直接命中，省 IO 也省时间。
    # 写操作不缓存——文件内容会变。
    cache_key = ""
    if name in READ_ONLY_TOOLS:
        import json as _json
        cache_key = name + "|" + _json.dumps(args, sort_keys=True, ensure_ascii=False)
        if cache_key in state.cache:
            result = state.cache[cache_key] + "\n（缓存命中，未重新读取）"
            state.log.append({"tool": name, "args": args,
                              "preview": "（缓存）" + result[:140].replace("\n", " ")})
            return result, stuck

    result = tools_mod.call(name, args)
    if cache_key:
        state.cache[cache_key] = result
    state.log.append({"tool": name, "args": args,
                      "preview": result.replace("\n", " ")[:160]})

    # 同一调用重复：**先警告，别急着判死刑**。
    # 原来第 3 次相同就直接中断，用户看到的是一句"任务卡住了"、
    # 什么也没拿到（实测：md 转 docx 的任务就这样被掐掉）。
    # 现在第 1 次重复只警告并要求换路，连续 2 次（≈6 次相同）才放弃。
    if stuck:
        state.stuck_hits += 1
        if state.stuck_hits < 2:
            stuck = False
            result += ("\n\n⚠️ 你刚刚用**同样的参数**调了同一个工具，结果不会变。"
                       "**换一条路**：换工具（比如 office(action='md2docx') 直接转，"
                       "不要自己拼 spec）、换参数、或者先 read_file 看清楚再动手。"
                       "再原地重复一次我就只能停下了。")
        else:
            result += ("\n\n⛔ 又是同一个调用。按当前方式做不下去，已经停下。")

    # 失败计数：工具返回 [错误]/[拒绝]/[超时] 都算。
    # 连续两次失败就明确要求换方法——30B 会反复用同一个错参数重试。
    failed = result.lstrip().startswith(("[错误]", "[拒绝]", "[超时]"))
    state.fails = state.fails + 1 if failed else 0
    if state.fails >= 2:
        result += (f"\n\n⚠️ 已经连续失败 {state.fails} 次。"
                   "**换方法再试**：换更短更具体的查询词、换工具（web_search ↔ fetch_url）、"
                   "换路径。**不要重复同样的调用，也不要把问题原样推回给用户**。"
                   "只有试过 2 种以上不同方法仍失败，才可以说做不到——"
                   "而且要说清是哪一步拿不到数据。")
    return result, stuck




# ---------------------------------------------------------------- 上下文压缩
# 30B 跑在 24 GB 上，上下文窗口只有 8k 左右。原值 9000 字符已经偏大：
# 实测 12 轮 + 大工具结果时，模型会直接返回空（上下文溢出），
# 被误判成 Metal OOM 去重启模型——白等两分钟。
CTX_CHAR_LIMIT = 7000      # 全部消息的字符上限，超了就压缩
KEEP_RECENT = 3            # 最近几轮工具结果保持完整


def _msg_len(msgs: list[dict]) -> int:
    n = 0
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        for tc in (m.get("tool_calls") or []):
            n += len(str(tc.get("function", {}).get("arguments", "")))
    return n


def compress(msgs: list[dict]) -> int:
    """历史太长时把早期的工具结果换成摘要，返回压缩掉的字符数。

    为什么必须做：30B 占 20 GB，agent 每轮把工具结果全文塞回上下文，
    4 轮就能把 Metal 显存撑爆（实测 OOM：kIOGPUCommandBufferCallbackErrorOutOfMemory）。
    压缩不解决模型能力，但能让它多跑几轮。

    策略：从最早的工具结果开始截断，保留最近 KEEP_RECENT 个完整。
    """
    before = _msg_len(msgs)
    if before <= CTX_CHAR_LIMIT:
        return 0

    tool_idx = [i for i, m in enumerate(msgs) if m.get("role") == "tool"]
    to_trim = tool_idx[:-KEEP_RECENT] if len(tool_idx) > KEEP_RECENT else []

    for i in to_trim:
        c = msgs[i].get("content") or ""
        if len(c) > 240:
            msgs[i]["content"] = c[:200] + "\n…（早期结果已压缩，需要细节就重新调用工具）"

    # 还不够就砍更早的 assistant 正文
    if _msg_len(msgs) > CTX_CHAR_LIMIT:
        for i, m in enumerate(msgs):
            if i == 0:
                continue
            c = m.get("content")
            if m.get("role") == "assistant" and isinstance(c, str) and len(c) > 300:
                m["content"] = c[:240] + "…"

    return before - _msg_len(msgs)


WRITE_TOOLS = {"write_file", "make_dir", "move_file"}
READ_ONLY_TOOLS = {"read_file", "read_files", "list_dir", "find_files"}


def changed_files(state: AgentState) -> list[str]:
    """本次任务改动过的路径。让用户在 done 时一眼看到"动了哪些东西"。

    没有这个的话，模型说"完成了"，用户并不知道它写了 5 个文件还是 1 个。
    """
    out = []
    for rec in state.log:
        if rec.get("tool") in WRITE_TOOLS:
            p = (rec.get("args") or {}).get("path") or (rec.get("args") or {}).get("dst")
            if p and p not in out:
                out.append(str(p))
    return out


def completion_note(state: AgentState) -> str:
    """任务结束时检查计划是否走完。没走完就明确标出来，不要假装完成了。"""
    if not state.plan:
        return ""
    remaining = len(state.plan) - state.cursor - 1
    if remaining <= 0:
        return ""
    return (f"\n\n⚠️ 注意：计划共 {len(state.plan)} 步，只完成到第 "
            f"{state.cursor + 1} 步，还有 {remaining} 步没做。")


def fallback_answer(state: AgentState) -> str:
    """轮次用尽/模型没给正文时的兜底总结。

    为什么必须有：实测过"12 轮全在 curl 抓同一个网页，最后 answer 是空字符串"，
    用户界面上一片空白——比说"没做成"还糟，因为完全不知道发生了什么。
    这里用任务日志拼一份"试了什么、卡在哪、下一步建议"，保证不空手。
    """
    from collections import Counter

    if not state.log:
        return ("这次没能开始执行工具。可能模型没理解需求，"
                "试着把任务拆小一点、或者换个更具体的说法。")

    cnt = Counter(r.get("tool") or "?" for r in state.log)
    tried = "、".join(f"{k}×{v}" for k, v in cnt.most_common(6))
    bad = [r for r in state.log
           if str(r.get("preview") or "").lstrip().startswith(
               ("[错误]", "[拒绝]", "[超时]", "[没搜到]"))]

    out = ["**这次没能在轮次内做完，把过程交代清楚。**", ""]
    if state.plan:
        out.append(f"计划 {len(state.plan)} 步，做到第 {min(state.cursor + 1, len(state.plan))} 步。")
    out.append(f"试过：{tried}")
    if bad:
        out.append("")
        out.append("卡住的地方：")
        for r in bad[:5]:
            pv = str(r.get("preview") or "").replace("\n", " ")[:90]
            out.append(f"- `{r.get('tool')}` → {pv}")
    out += ["", "**下一步建议**：",
            "1. 把需求拆细（比如先只问「RTX 5070 现在多少钱」，拿到数字再要图）",
            "2. 或者直接给一个具体网址，我抓那一页的正文来取数"]
    return "\n".join(out)


SEARCH_TOOLS = {"web_search", "fetch_url", "knowledge_search"}


def build_system(base: str, state: AgentState) -> str:
    block = state.system_block()

    # 收口提醒：30B 会一直搜、永远不画图（实测 12 轮全在 search，一句结论都没有）。
    # 光靠提示词约束不住，这里按日志计数硬性提醒，把它推到交付那一步。
    n_search = sum(1 for r in state.log if r.get("tool") in SEARCH_TOOLS)
    has_chart = any(r.get("tool") == "make_chart" for r in state.log)
    if n_search >= 3 and not has_chart:
        block += (f"\n\n【收口提醒】你已经搜了 {n_search} 次，信息够了。"
                  "**下一步必须调用 make_chart 出图**（用搜到的数字），"
                  "然后把图和数字写进回答。不要再搜了。")
    elif n_search >= 5 and has_chart:
        block += ("\n\n【收口提醒】图已经出过了，**直接写结论收尾**，"
                  "把价格数字、来源和局限写清楚，不要再调工具。")
    return base + ("\n\n" + block if block else "")


# ---------------------------------------------------------------- 子代理
async def run_task(goal: str, system_base: str, tools_mod, *,
                   model: str, url: str, headers: dict | None = None,
                   max_turns: int = 6, temperature: float = 0.5,
                   max_tokens: int = 1024) -> dict:
    """非流式跑一个独立 agent 任务（供 spawn_agents 并行派发）。

    和主 agent 循环的区别：
    - 独立 AgentState、独立 messages（上下文互不污染）
    - 非流式（子任务不需要边跑边推给用户，只要结果）
    - 不带 spawn 工具（避免无限递归）
    """
    import asyncio

    import httpx

    state = AgentState(goal=goal[:200])
    stuck_hits = 0
    toolset = all_tools(tools_mod.TOOLS)
    msgs = [{"role": "system", "content": system_base},
            {"role": "user", "content": goal}]

    for turn in range(max(1, min(max_turns, 10))):
        msgs[0]["content"] = build_system(system_base, state)
        compress(msgs)

        body = {"model": model, "messages": msgs, "tools": toolset,
                "temperature": temperature, "max_tokens": max_tokens,
                # 必须走流式：mlx_lm.server 在**非流式**分支里会把 tool_calls 丢掉
                # （返回 finish_reason=tool_calls 但 message 里没有 tool_calls、
                #  content 为 null）。子代理因此永远看不到工具调用，只会空转。
                "stream": True}
        text_acc: list[str] = []
        tool_acc: dict[int, dict] = {}
        try:
            async with httpx.AsyncClient(timeout=600, trust_env=False) as c:
                async with c.stream("POST", url, json=body, headers=headers or {}) as r:
                    if r.status_code >= 400:
                        return {"ok": False, "answer": f"上游 HTTP {r.status_code}",
                                "turns": turn + 1}
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        raw = line[6:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            d = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        delta = (d.get("choices") or [{}])[0].get("delta") or {}
                        if delta.get("content"):
                            text_acc.append(delta["content"])
                        for tc in (delta.get("tool_calls") or []):
                            i = tc.get("index", 0)
                            slot = tool_acc.setdefault(i, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
        except httpx.HTTPError as e:
            return {"ok": False, "answer": f"上游调用失败：{type(e).__name__}", "turns": turn + 1}

        msg = {"role": "assistant", "content": "".join(text_acc) or None}
        if tool_acc:
            msg["tool_calls"] = [
                {"id": v["id"] or f"call_{i}", "type": "function",
                 "function": {"name": v["name"], "arguments": v["arguments"] or "{}"}}
                for i, v in sorted(tool_acc.items())
            ]

        msgs.append(msg)
        calls = msg.get("tool_calls") or []

        if not calls:
            ans = (msg.get("content") or "").strip()
            if ans:
                return {"ok": True, "answer": ans, "turns": turn + 1,
                        "changed": changed_files(state)}
            continue          # 空回复，重试下一轮

        for tc in calls:
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            if name in ("write_plan", "spawn_agents"):       # 子代理不做规划/再派发
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            used = {r.get("tool") for r in state.log}
            blocked = guard.guard_write(state.goal, name, args, used) or guard.guard_tool(
                state.goal, name, args)
            if blocked:
                result = blocked
            else:
                result, stuck = execute(state, name, args, tools_mod)
                if name in guard.WRITE_TOOLS and not result.startswith(("[错误]", "[拒绝]", "[拦截]")):
                    result += guard.readback(tools_mod.call, args)
                if stuck:
                    # 原来 run_task 拿到 stuck 标志却直接丢掉，模型会一路重复到轮数用尽。
                    stuck_hits += 1
                    result += ("\n[卡住] 同一个调用已经连续 3 次了，这个方法不 work。"
                               "换个做法：路径用绝对路径、或者换一个工具。")
                    if stuck_hits >= 2:
                        record(state, name, args, result)
                        return {"ok": False, "answer": "连续卡在同一个操作上，主动放弃（不假装完成）",
                                "turns": turn + 1, "changed": changed_files(state)}
            record(state, name, args, result)
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": result[:2000]})

    return {"ok": False, "answer": "（达到最大轮数，任务未完成）",
            "turns": max_turns, "changed": changed_files(state)}
