"""把本机状态变成一段短文本，注入 system prompt。

## 为什么不做成工具调用

工具调用要配 agent loop（模型输出 tool_calls → 执行 → 回灌 → 再循环），
为了问一句"现在内存够跑 30B 吗"要付整套循环的代价，不划算。

固定注入的成本是**每轮多 ~80 token**：prefill 多 3~4 秒（本地）。
换来的是模型永远知道真实状态——不会编造"你的内存应该够"这种话。

## 数据源

全部走 psutil，加一个 runner 的当前模型名。
耗时 < 5 ms，不值得缓存。
"""
from __future__ import annotations

import time

import psutil


def _gb(n: float) -> float:
    return round(n / 1024**3, 1)


def snapshot(include_model: bool = True) -> str:
    """生成一段紧凑的状态描述。约 80 token。"""
    try:
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
        # macOS 的 / 是只读系统卷（约 13 GB），真实占用在 Data 卷。
        # 用 / 会报出 12.7/228 GB 这种明显不对的数字。
        import os
        _root = "/System/Volumes/Data" if os.path.isdir("/System/Volumes/Data") else "/"
        du = psutil.disk_usage(_root)
        cpu = psutil.cpu_percent(interval=None)      # 模块级状态，首次返回 0
        cores = psutil.cpu_count()
    except Exception:                                 # noqa: BLE001
        return ""

    # 必须带年份和星期：只写 "11-03 12:32" 时，模型会按训练期的年份回答
    # （实测被问"今天的日期"，它答成了 2023 年）。
    _wd = "一二三四五六日"[time.localtime().tm_wday]
    lines = [
        f"【本机状态 · {time.strftime('%Y-%m-%d %H:%M')} 星期{_wd}】",
        f"内存 {_gb(vm.total - vm.available)}/{_gb(vm.total)} GB 已用，"
        f"可用 {_gb(vm.available)} GB",
    ]
    if sw.total:
        lines.append(f"Swap {_gb(sw.used)}/{_gb(sw.total)} GB"
                     + ("（偏高，说明之前内存不够过）" if sw.percent > 50 else ""))
    lines.append(f"磁盘 {_gb(du.used)}/{_gb(du.total)} GB（{du.percent:.0f}%）")
    lines.append(f"CPU {cpu:.0f}% · {cores} 核")

    if include_model:
        try:
            from .runner import runner
            st = runner.status()
            model = st.get("model") if st.get("state") == "ready" else None
            if model:
                lines.append(f"正在运行的本地模型：{model}")
            else:
                lines.append("当前没有模型在运行")
        except Exception:                             # noqa: BLE001
            pass

    return "\n".join(lines)


def as_system() -> str:
    """注入用的完整片段。空数据时返回空串，不污染 prompt。"""
    snap = snapshot()
    if not snap:
        return ""
    return (snap + "\n\n"
            "回答涉及性能、内存、能否运行某个模型的问题时，以这段真实数据为准，"
            "不要凭印象估算。"
            "涉及今天日期、星期几、当前时间的问题，**以上面的时间戳为准**"
            "（这是真实系统时间，不是你的训练数据），不要说「我的知识截止到…」。")
