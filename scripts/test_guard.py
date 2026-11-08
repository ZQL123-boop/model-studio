"""guard_tool 回归测试：真要图的请求不能被误拦。

复现事故（2026-11-08）：用户说「生成一张猫咪的照片」，旧版连续子串
白名单 ("画","生成图","来张",...) 匹配不到带量词的表达，guard 返回
「[拦截] 用户没有要求生成图片。」，本地 30B 收到后道歉并放弃任务，
26.8 秒只换来一句「我之前错误地调用了生成图片的工具」。

跑法：.venv/bin/python scripts/test_guard.py
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import guard  # noqa: E402

# 用户明确要图 → 必须放行（None）
SHOULD_PASS = [
    "生成一张猫咪的照片",          # 事故原句
    "给我一张猫咪照片",
    "生成一张猫咪的图",
    "画一只猫",
    "生成图片：一只猫",
    "来张猫的图",
    "做一张海报",
    "给我画个头像",
    "换张壁纸",
    "整一张插画",
    "a photo of a cat",
    "生成一张日落配图",
    "再生成一张",                  # 多轮指代：不能因为没写「图」就拦
    "再来一张",
    "又生成一张",
]

# 明确的纯文本任务 → 必须拦（模型自己加戏生图）
SHOULD_BLOCK = [
    "ACM 的 C 语言算法题清单",      # 注释里记录的原始事故
    "整理一份清单",
    "汇总一下上面的内容",
    "翻译这段话",
    "",
]

# make_chart 的规则不能被这次改动带坏
CHART_CASES = [
    ("画一张销售趋势图", '{"x":[1,2,3],"y":[10,20,30]}', None),   # 有数值 → 放行
    ("画一张销售趋势图", "随便写点文字", "拦截"),                    # 无数值 → 拦
    ("整理一份清单", '{"x":[1,2,3]}', "拦截"),                      # 没要图表 → 拦
]


def run() -> int:
    failed = 0

    for goal in SHOULD_PASS:
        got = guard.guard_tool(goal, "generate_image", {"prompt": "cat"})
        if got is not None:
            print(f"FAIL 应放行却被拦: {goal!r} -> {got}")
            failed += 1

    for goal in SHOULD_BLOCK:
        got = guard.guard_tool(goal, "generate_image", {"prompt": "cat"})
        if got is None:
            print(f"FAIL 应拦截却放行: {goal!r}")
            failed += 1
        elif "不要解释这次拦截" not in got:
            print(f"FAIL 拦截语没给下一步指令（模型会道歉摆烂）: {goal!r} -> {got}")
            failed += 1

    for goal, spec, expect in CHART_CASES:
        got = guard.guard_tool(goal, "make_chart", {"spec": spec})
        if expect is None and got is not None:
            print(f"FAIL make_chart 应放行: {goal!r} -> {got}")
            failed += 1
        if expect == "拦截" and got is None:
            print(f"FAIL make_chart 应拦截: {goal!r}")
            failed += 1

    total = len(SHOULD_PASS) + len(SHOULD_BLOCK) + len(CHART_CASES)
    print(f"\n{total - failed}/{total} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
