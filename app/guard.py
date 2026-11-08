"""交付前自检：两个钩子，专治「看起来跑完了」。

1. **写入口令**：目标要的是汇总/计算出来的数字，但此前一次计算工具都没调过
   → 拦下写入，把模型推回去真算。实测本地 30B 会跳过计算、直接把猜的数字
   写进文件，再回一句「已完成」。
2. **写后回读**：写完立刻把磁盘内容读回来附在工具结果里，让「到底写进去了什么」
   变成可见事实，而不是模型的自述。

两个补丁都在 harness 层，不依赖模型自觉——模型不会因为变大就学会对账。
"""
from __future__ import annotations

import re

COMPUTE_TOOLS = {"run_bash"}

# 出现这些词，说明要的是「算出来的值」，不是抄一个文本里已有的数
DERIVED_WORDS = (
    "总额", "总销售额", "合计", "总计", "总和", "求和", "平均",
    "差值", "总价", "累计", "换算成", "汇率换算",
)

_DIGIT = re.compile(r"\d")
WRITE_TOOLS = ("write_file", "edit_file")


def needs_derived_number(goal: str) -> bool:
    g = goal or ""
    return any(w in g for w in DERIVED_WORDS)


def used_compute(used) -> bool:
    return bool(set(used or ()) & COMPUTE_TOOLS)


def guard_write(goal: str, name: str, args: dict, used) -> str | None:
    """写入前拦截；返回 None 表示放行。"""
    if name not in WRITE_TOOLS:
        return None
    if not needs_derived_number(goal) or used_compute(used):
        return None
    content = str((args or {}).get("content") or "")
    if not _DIGIT.search(content):
        return None
    return ("[拦截] 目标是算出来的金额，但此前没调用过任何计算工具（run_bash），"
            "写进去的数字只能是猜的。\n"
            "下一步：调用 run_bash 执行 `python3 -c \"...\"`（用 csv 标准库，"
            "不要 pip install，环境里没有外网包），把结果打印出来，拿到真实数字再写文件。")


def readback(call, args: dict) -> str:
    """写完立刻回读，返回附加在工具结果后的校验文本。"""
    path = str((args or {}).get("path") or "")
    if not path:
        return ""
    try:
        got = call("read_file", {"path": path, "max_lines": 40})
    except Exception as e:                                    # noqa: BLE001
        return f"\n【回读】失败：{type(e).__name__}: {e}"
    return f"\n【回读校验】磁盘上 {path} 的实际内容：\n{got[:400]}"


# ---------------------------------------------------------------- 工具级拦截
CHART_WORDS = ("画图", "图表", "可视化", "柱状", "折线", "饼图", "散点",
               "趋势图", "画个图", "做成图", "出图", "画一张")

import re as _re

_DIGITS = _re.compile(r"\d")

# 用户「要一张图」的意图。中文里动作和名词之间常插量词/修饰语：
#   "生成一张猫咪的照片"、"给我一张猫图"
# 旧版用连续子串白名单（"生成图"）匹配不到这种结构，把真要图的请求判成
# 「没要求图片」→ 工具被拦 → 本地 30B 收到拦截语后道歉并放弃整个任务。
_IMAGE_HINT = _re.compile(
    r"画|绘|"
    r"照片|图片|图像|插画|海报|配图|壁纸|头像|封面|表情包|"
    r"(?:生成|做|弄|来|给|出|要|整|搞|换)[^，。！？\n]{0,8}[图画像片照]|"
    r"[张幅][^，。！？\n]{0,8}[图画像片照]"
)

# 明确的纯文本任务：只有这些才拦「模型自己加戏生图」。
_TEXT_ONLY_TASK = (
    "清单", "整理", "汇总", "列表", "列一下", "列出来", "总结", "表格",
    "写成", "写一段", "翻译", "解释", "说明", "写代码", "脚本",
)


def wants_chart(goal: str) -> bool:
    g = goal or ""
    return any(w in g for w in CHART_WORDS)


def wants_image(goal: str) -> bool:
    return bool(_IMAGE_HINT.search(goal or ""))


def guard_tool(goal: str, name: str, args: dict) -> str | None:
    """拦"用户没要、模型自己加戏"的工具调用。返回 None 表示放行。

    实测事故：用户要"ACM 的 C 语言算法题清单"，模型画了一张"ACM 参赛资格"
    的柱状图，连着失败 3 次还在试——用户要的压根不是图。

    代价是不对称的：误杀（用户要图却被拦，只收到一句道歉、任务彻底失败）
    远严重于漏放（模型多跑 1 秒生图）。所以「拿不准」一律放行，只有明确
    是纯文本任务才拦。
    """
    if name == "make_chart":
        if not wants_chart(goal):
            return ("[拦截] 用户没有要求图表。整理、汇总、列清单请直接用 markdown 表格，"
                    "不要调用 make_chart。")
        spec = str((args or {}).get("spec") or "")
        if len(_DIGITS.findall(spec)) < 3:
            return ("[拦截] make_chart 需要数值数据（x 轴 + 一列数字），你给的是文字，"
                    "画不出来。整理类任务请直接用表格回答。")
    if name == "generate_image":
        g = goal or ""
        if wants_image(g):
            return None
        if any(w in g for w in _TEXT_ONLY_TASK) or not g.strip():
            # 别只说「用户没要求图片」：本地 30B 会据此道歉然后直接结束，
            # 连原本的文字任务都不做。必须给明确的下一步指令。
            return ("[拦截] 用户要的是文字结果，不是图片。不要调用 generate_image，"
                    "也不要解释这次拦截或向用户道歉——直接按要求用文字回答。")
    return None
