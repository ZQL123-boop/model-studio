"""自动选模型：给"自动"这个选项一个说得清、可复现的规则。

## 为什么需要

界面里现在要手动选：本地 30B / DeepSeek Chat / DeepSeek Reasoner。
用户不知道哪个合适——选本地怕答不好，选 Reasoner 怕慢，结果每次都纠结。

自动模式要在**质量、速度、成本**三者之间做取舍，规则必须能解释：

    "你好"                    → 本地（免费、1 秒）
    "树状数组 lowbit 原理"      → 本地（知识题，30B 够）
    "帮我证明这个贪心为什么对"   → DeepSeek Reasoner（要推理链）
    "把这 300 行代码重构一下"    → DeepSeek Chat（长上下文 + 代码强）
    本地没启动 / 崩了           → 有 key 就走 API，别让用户干等 30 秒冷启动

## 为什么用关键词而不是分类模型

分类模型要额外加载（30B 已经占 17 GB），而且不可解释。
关键词法有误差，但每次都会在界面上显示"为什么选它"，
选错了用户能立刻看懂并手动覆盖——这比一个黑盒分数更实用。
"""
from __future__ import annotations

# 需要推理链的：证明、推导、为什么、复杂度、权衡
# 注意：调试/重构属于"写代码"，交给 Chat 更快，别放这里
REASON_WORDS = (
    "证明", "推导", "为什么", "为啥", "复杂度", "正确性", "反例",
    "优化到", "时间限制", "权衡", "取舍", "架构", "设计一个", "设计个",
    "数学", "算法导论", "动态规划转移", "想不明白", "内存泄漏", "死锁",
)

# 代码/长文/整理类：本地 30B 也行，但 API 更稳、更快
CODE_WORDS = (
    "代码", "函数", "写个", "写一个", "实现", "脚本", "正则", "报错",
    "单元测试", "接口", "sql", "html", "css", "javascript", "python",
    "java", "c++", "rust", "重构", "review", "code", "调试", "排查",
)

GREETING_MAX = 6          # 这么短的输入，不值得动大模型
LONG_TEXT = 800           # 超过这么多字，本地上下文吃紧


def _has(text: str, words: tuple[str, ...]) -> str:
    low = text.lower()
    for w in words:
        if w.lower() in low:
            return w
    return ""


def pick(text: str, *, local_ready: bool, has_chat_api: bool,
         has_reason_api: bool) -> tuple[str, str]:
    """返回 (provider_id, 理由)。provider_id 为空串表示本地。

    规则顺序即优先级：先看"这活本地干得了吗"，再看"要不要推理链"。
    """
    t = (text or "").strip()

    if not t:
        return "", "空输入，用本地"

    # 1) 本地没起来：能走 API 就别让用户等冷启动（30~40 秒）
    if not local_ready:
        if has_chat_api:
            return "api__deepseek", "本地模型未启动，先用 DeepSeek（免等冷启动）"
        return "", "本地模型未启动，且没有可用的 API key，自动拉起本地"

    # 2) 超短寒暄：本地足够，别浪费钱
    if len(t) <= GREETING_MAX and not _has(t, CODE_WORDS):
        return "", "短问答，本地就够"

    # 3) 要推理链的：Reasoner
    hit = _has(t, REASON_WORDS)
    if hit and has_reason_api:
        return "api__deepseek_r1", f"提到「{hit}」，需要推理链 → Reasoner"

    # 4) 代码 / 长文：Chat API
    hit = _has(t, CODE_WORDS)
    if (hit or len(t) >= LONG_TEXT) and has_chat_api:
        why = f"提到「{hit}」，代码类交给 DeepSeek" if hit else f"输入 {len(t)} 字偏长，避开本地上下文上限"
        return "api__deepseek", why

    # 5) 其余：本地（免费、隐私、速度可接受）
    return "", "知识与日常问答，本地 30B 足够"
