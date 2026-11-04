"""对话记忆：把每次问答落盘，并提供可配置的上下文深度。

## 为什么需要

之前的对话只在浏览器内存里（`state.messages`），刷新就没了，
连日志里都没有请求体——聊过的东西一条都找不回来。

## 存什么

    data/memory.json          设置（开关 / 深度 / 上限）
    data/chat_history.jsonl   每条一行，追加写

只存问题、答案、耗时、模型名。**不存 thinking 过程**——推理模型的思考链
动辄上千 token，存下来文件会膨胀十倍，而回看时几乎没人读思考。

## 记忆怎么生效

`depth` 控制发送给模型的上下文轮数（1 轮 = 一问一答）。
前端 `send()` 里只取 `state.messages.slice(-depth * 2)`，
不是把全部历史塞进去——否则长对话会把 8k 上下文撑爆。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HIST = DATA / "chat_history.jsonl"
SETTINGS = DATA / "memory.json"
NOTES = DATA / "notes.md"          # 手写的长期记忆，纯 Markdown

DEFAULTS = {
    "enabled": True,      # 是否把历史带给模型
    "depth": 6,           # 带最近几轮（一问一答算一轮）
    "max_records": 1000,  # 磁盘上最多保留多少条
    "sysinfo": True,      # 每轮把本机状态注入 system（约 80 token）
}

_LOCK = Lock()


def _ensure() -> None:
    DATA.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    _ensure()
    if not SETTINGS.exists():
        return dict(DEFAULTS)
    try:
        d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    out.update({k: d[k] for k in DEFAULTS if k in d})
    # 收敛到合理区间，手改坏了也不至于把界面搞崩
    out["depth"] = max(0, min(int(out["depth"]), 50))
    out["max_records"] = max(50, min(int(out["max_records"]), 50000))
    out["enabled"] = bool(out["enabled"])
    out["sysinfo"] = bool(out["sysinfo"])
    return out


def save_settings(patch: dict) -> dict:
    cur = load_settings()
    for k in DEFAULTS:
        if k in patch and patch[k] is not None:
            cur[k] = patch[k]
    cur["depth"] = max(0, min(int(cur["depth"]), 50))
    cur["max_records"] = max(50, min(int(cur["max_records"]), 50000))
    cur["enabled"] = bool(cur["enabled"])
    cur["sysinfo"] = bool(cur["sysinfo"])
    _ensure()
    SETTINGS.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def record(question: str, answer: str, *, model: str = "",
           elapsed_s: float = 0.0, completion_tokens: int | None = None,
           ttft_s: float = 0.0) -> None:
    """追加一条问答。答案为空（比如调用失败）时不记。"""
    q = (question or "").strip()
    a = (answer or "").strip()
    if not q or not a:
        return
    rec = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "question": q,
        "answer": a,
        "elapsed_s": round(elapsed_s, 2),
        "ttft_s": round(ttft_s, 2),
        "completion_tokens": completion_tokens,
    }
    _ensure()
    with _LOCK:
        with HIST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim(load_settings()["max_records"])


def _trim(max_records: int) -> None:
    """超过上限就重写文件，只留最近 max_records 条。调用方已持锁。"""
    try:
        lines = HIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    lines = [l for l in lines if l.strip()]
    if len(lines) <= max_records:
        return
    HIST.write_text("\n".join(lines[-max_records:]) + "\n", encoding="utf-8")


def history(limit: int = 50) -> dict:
    """最近的问答，时间倒序（新的在前，界面上不用再翻转）。"""
    _ensure()
    if not HIST.exists():
        return {"count": 0, "records": []}
    try:
        lines = [l for l in HIST.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return {"count": 0, "records": []}
    total = len(lines)
    out = []
    for line in reversed(lines[-max(1, limit):]):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"count": total, "records": out}


def context_for_model(depth: int) -> list[dict]:
    """取最近 depth 轮，转成 OpenAI messages 格式，供前端拼上下文。"""
    if depth <= 0:
        return []
    data = history(limit=depth)[ "records" ]       # 新的在前
    msgs: list[dict] = []
    for rec in reversed(data):                      # 改回时间正序
        msgs.append({"role": "user", "content": rec.get("question", "")})
        msgs.append({"role": "assistant", "content": rec.get("answer", "")})
    return msgs


NOTES_MAX = 2000          # 字符上限。超过 2000 字符会明显挤占上下文


def load_notes() -> dict:
    """手写的长期记忆。像 WorkBuddy 的 MEMORY.md：不自动生成，全靠人写。"""
    _ensure()
    text = ""
    if NOTES.exists():
        try:
            text = NOTES.read_text(encoding="utf-8")
        except OSError:
            text = ""
    return {"text": text, "chars": len(text), "max_chars": NOTES_MAX,
            "path": str(NOTES)}


def save_notes(text: str) -> dict:
    _ensure()
    text = (text or "")[:NOTES_MAX]          # 截断而不是报错，别让人白写
    NOTES.write_text(text, encoding="utf-8")
    return load_notes()


def notes_as_system() -> str | None:
    """转成 system prompt 片段。空文件返回 None，不注入。"""
    t = load_notes()["text"].strip()
    if not t:
        return None
    return ("以下是用户的背景信息，仅作为回答时的参考。\n"
            "**不要主动复述这些内容**——它们不是用来介绍自己的，\n"
            "只有在用户明确问起相关的事时才引用。\n"
            "```\n" + t + "\n```")


def clear() -> int:
    """清空历史，返回删除条数。"""
    _ensure()
    if not HIST.exists():
        return 0
    with _LOCK:
        try:
            n = len([l for l in HIST.read_text(encoding="utf-8").splitlines() if l.strip()])
        except OSError:
            n = 0
        HIST.write_text("", encoding="utf-8")
    return n


def stats() -> dict:
    _ensure()
    try:
        size = HIST.stat().st_size if HIST.exists() else 0
        n = len([l for l in HIST.read_text(encoding="utf-8").splitlines() if l.strip()]) if size else 0
    except OSError:
        size, n = 0, 0
    return {
        "settings": load_settings(),
        "count": n,
        "size_kb": round(size / 1024, 1),
        "path": str(HIST),
    }
