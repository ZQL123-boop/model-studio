"""write_plan 回归：cursor 到末尾不能越界，原地踏步要收口。

复现（2026-11-09 21:27 截图）：问「用c语言写穷举法求最大公约数」，
agent 连调 6 次 write_plan，最后抛 IndexError: list index out of range，
124 秒白跑，用户什么也没拿到。

拿不到计划的正文都行，但不能崩、也不能无限刷。
跑法：.venv/bin/python scripts/test_plan.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import agent, tools  # noqa: E402

failed = 0
total = 0


def check(cond: bool, label: str):
    global failed, total
    total += 1
    if not cond:
        print(f"FAIL {label}")
        failed += 1


STEPS = ["分析需求", "写代码", "自测"]


def run(st, current, steps=STEPS):
    return agent.execute(st, "write_plan", {"steps": steps, "current": current}, tools)


# 1) 正常推进
st = agent.AgentState(goal="写个求最大公约数的程序")
r, stuck = run(st, 0)
check(r.startswith("[计划已更新]") and not stuck, "首次列计划应成功")
check(st.cursor == 0 and len(st.plan) == 3, "cursor/plan 应写入")

# 2) cursor 直接拉到 len：原来这里 IndexError
r, stuck = run(st, 3)
check("IndexError" not in r, "cursor 等于 len 不能越界")
check(st.cursor == 3, "cursor 应允许到 len（表示全部完成）")
check("计划已全部完成" in r, "末尾应给出完成态文案")

# 3) 完成后再重复调：应直接收口
r, stuck = run(st, 3)
check(stuck is True, "计划全部完成还重复调，应 stuck 收口")
check("不要再调 write_plan" in r, "收口文案要明确禁止再调")

# 4) 原地踏步：连续 3 次才收口，前两次只提示
st2 = agent.AgentState(goal="写个程序")
r0, _ = run(st2, 0, ["a", "b", "c", "d"])
check(r0.startswith("[计划已更新]"), "准备一个 4 步计划")
r1, s1 = run(st2, 0, ["a", "b", "c", "d"])
r2, s2 = run(st2, 0, ["a", "b", "c", "d"])
r3, s3 = run(st2, 0, ["a", "b", "c", "d"])
check(not s1 and "不要重复列计划" in r1, "第 1 次重复：只提示")
check(not s2, "第 2 次重复：仍只提示")
check(s3 is True and "停止调计划" in r3, "第 3 次重复：收口")

# 5) 空计划不崩
st3 = agent.AgentState(goal="x")
r, _ = run(st3, 0, [])
check("IndexError" not in r, "空 steps 不能崩")

# 6) 越界保护函数本身
st4 = agent.AgentState(goal="x")
st4.plan = ["只有一步"]
st4.cursor = 1
check(agent._plan_step_text(st4) == "（计划已全部完成）", "越界取步骤应返回占位文本")

print(f"\n{total - failed}/{total} 通过")
raise SystemExit(1 if failed else 0)
