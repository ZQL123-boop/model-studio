"""评测报告（2026-11-08）缺陷修复的回归测试。

覆盖：
  1. 高危 #1 访客工具白名单：只在 schema 层过滤不够，执行前必须拦住
  2. 高危 #2 set_goal 编造写入 / 自证污染
  3. 中 #4 单次工具失败即给换方法推力
  4. 中 #3 RAG 弱命中显式提示换路
  5. 中 #6 / 低 #8 提示词与 schema 描述

全部离线，不调真实模型、不写真实目标库。
跑法：.venv/bin/python scripts/test_eval_fixes.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import agent, guard, main, persona, tools  # noqa: E402

failed = 0
total = 0


def check(cond: bool, label: str) -> None:
    global failed, total
    total += 1
    if not cond:
        print(f"FAIL {label}")
        failed += 1


# ---------------------------------------------------------------- 1. 访客白名单
tools.set_visitor_allowed(main.REMOTE_TOOLS)
try:
    for nm in ("set_goal", "run_bash", "edit_file", "spawn_agents", "design"):
        r = tools.call(nm, {})
        check(r.startswith("[拒绝]") and "远程访客" in r,
              f"访客应拦住 {nm}")

    r = tools.call("read_file", {"path": "/definitely_not_here"})
    check(not r.startswith("[拒绝] 工具"), "白名单内只读工具应放行（可报文件不存在）")

    async def _par():
        return await asyncio.to_thread(tools.call, "set_goal", {"goal": "不该写"})
    check("远程访客" in asyncio.run(_par()),
          "asyncio.to_thread 并行分支也必须拦住（ContextVar 要能传递）")
finally:
    tools.set_visitor_allowed(None)

check("没有这个工具" in tools.call("no_such_tool", {}),
      "解除访客模式后本机请求恢复正常")

# ---------------------------------------------------------------- 2. set_goal 护栏
c7_goal = "我有哪些长期目标？"
check(guard.guard_tool(c7_goal, "set_goal",
                       {"goal": "每周学习新的编程语言"},
                       last_result="还没有目标。用 set_goal 记录一个。") is not None,
      "C7：紧接「没有目标」的写库必须拦")
check(guard.guard_tool(c7_goal, "set_goal",
                       {"goal": "提升英语水平"}) is not None,
      "C7：用户只查询时，编造的目标必须拦")
check(guard.guard_tool("帮我记一个目标：每周学习新的编程语言", "set_goal",
                       {"goal": "每周学习新的编程语言"}) is None,
      "用户明确给出目标时应放行")
check(guard.guard_tool("设定目标：每天跑步 3 公里", "set_goal",
                       {"goal": "每天跑步 3 公里"}) is None,
      "换个说法但内容出自原话时应放行")
check(guard.guard_tool(c7_goal, "set_goal",
                       {"goal": "随便", "done": True}) is None,
      "done=True 是标记已有目标完成，不该被拦")
check(not guard.grounded_in_user("每周学习新的编程语言", c7_goal),
      "编造文本与用户原话的字级 bigram 覆盖率应低于 0.6")
check(guard.grounded_in_user("每天跑步 3 公里", "记住目标：每天跑步 3 公里"),
      "原话里的目标应判为 grounded")

# ---------------------------------------------------------------- 3. 首次失败换路
with tempfile.TemporaryDirectory():
    st = agent.AgentState(goal="读一个不存在的文件")
    r, _ = agent.execute(st, "read_file", {"path": "/definitely_not_here"}, tools)
    check("先换方法再试一次" in r, "首次工具失败就该给换方法推力")
    check("find_files" in r and "list_dir" in r, "提示里要给出具体替代工具")

    st2 = agent.AgentState(goal="读一个不存在的文件")
    # 换不同路径：相同参数会命中只读缓存并提前 return，走不到失败计数
    agent.execute(st2, "read_file", {"path": "/nope_1"}, tools)
    r2, _ = agent.execute(st2, "read_file", {"path": "/nope_2"}, tools)
    check("连续失败 2 次" in r2, "连续失败仍走原来的强提示")

# ---------------------------------------------------------------- 4. RAG 弱命中
def fake_rag(score):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"chunks": [
        {"doc_id": "oi-wiki", "header_path": "图论/Bellman-Ford",
         "score": score, "body": "负权边相关段落"}]}
    c = MagicMock()
    c.post.return_value = resp
    C = MagicMock()
    C.return_value.__enter__.return_value = c
    return C

with patch("httpx.Client", fake_rag(0.1538)):
    r = tools.knowledge_search("负权图最短路径复杂度")
    check("[弱命中]" in r and "0.1538" in r, "top1 相似度低要显式标注")
    check("不要用先验知识把答案补齐" in r, "弱命中要禁止拿先验补齐")

with patch("httpx.Client", fake_rag(0.82)):
    r = tools.knowledge_search("负权图最短路径复杂度")
    check("[弱命中]" not in r, "高相似度不该误报弱命中")

# ---------------------------------------------------------------- 5. 提示词 / schema
check("推理链必须能逐句核对" in persona.as_system(with_tools=True),
      "STYLE 里要有可校验推理链约束")
read_schema = next(t["function"] for t in tools.TOOLS
                   if t["function"]["name"] == "read_file")
check("至少读 80 行" in read_schema["description"],
      "read_file 描述要挡住 max_lines=1 就总结")

print(f"\n{total - failed}/{total} 通过")
raise SystemExit(1 if failed else 0)
