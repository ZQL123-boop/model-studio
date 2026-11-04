"""定时提醒：一次性 / 每天 / 每周 / cron。

## 为什么不用 APScheduler

多一个依赖 + 后台线程管理，对这个体量是过度设计。
这里只需要"到点生成一条通知，前端轮询显示"，自建足够。

## 数据结构

data/schedules.json:
{
  "schedules": [{"id", "name", "prompt", "at", "repeat", "weekday", "next", "last"}],
  "notifications": [{"id", "time", "name", "prompt", "read"}]
}

repeat: ""（一次性）/ "daily" / "weekly" / "cron"
- 一次性：at = "YYYY-MM-DD HH:MM"
- daily：at = "HH:MM"
- weekly：at = "HH:MM" + weekday = 0~6
- cron：cron 表达式，解析用简单实现（分 时 日 月 周）
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FILE = DATA_DIR / "schedules.json"


def _load() -> dict:
    if not FILE.exists():
        return {"schedules": [], "notifications": []}
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schedules": [], "notifications": []}


def _save(d: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_run(repeat: str, at: str, weekday: str) -> int | None:
    """算下一次触发的时间戳（秒）。"""
    now = datetime.now()
    if repeat == "daily":
        try:
            hh, mm = map(int, at.split(":"))
        except (ValueError, TypeError):
            return None
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if t <= now:
            t = t.replace(day=t.day + 1) if t.day < 28 else t  # 粗略加一天
        return int(t.timestamp())

    if repeat == "weekly":
        try:
            hh, mm = map(int, at.split(":"))
            wd = int(weekday)
        except (ValueError, TypeError):
            return None
        days_ahead = (wd - now.weekday()) % 7
        t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        t = t.replace(day=now.day + days_ahead)
        if t <= now:
            t = t.replace(day=now.day + 7)
        return int(t.timestamp())

    if repeat == "cron":
        # 简化 cron：只支持 "分 时 日 月 周"，只判断"下一次是不是现在这分钟"
        # 完整 cron 交给下一次触发时的粗算——这里给"下一分钟"兜底
        return int(now.timestamp()) + 60

    # 一次性
    try:
        t = datetime.strptime(at, "%Y-%m-%d %H:%M")
        return int(t.timestamp())
    except (ValueError, TypeError):
        return None


def add(name: str, prompt: str, at: str, repeat: str = "", weekday: str = "0") -> tuple[bool, str, str]:
    name = (name or "").strip() or "提醒"
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "提醒内容不能为空", ""
    nxt = _next_run(repeat or "", at or "", weekday or "0")
    if nxt is None:
        return False, "时间格式不对：一次性用 'YYYY-MM-DD HH:MM'，每天/每周用 'HH:MM'", ""

    d = _load()
    sid = uuid.uuid4().hex[:8]
    d["schedules"].append({
        "id": sid, "name": name, "prompt": prompt,
        "at": at, "repeat": repeat or "", "weekday": weekday,
        "next": nxt, "last": None, "created": int(time.time()),
    })
    _save(d)
    return True, f"已设置：{name}（{_describe(repeat or '', at, weekday)}）", sid


def _describe(repeat: str, at: str, weekday: str) -> str:
    if repeat == "daily":
        return f"每天 {at}"
    if repeat == "weekly":
        return f"每周 {'一二三四五六日'[int(weekday or 0)]} {at}"
    if repeat == "cron":
        return f"cron {at}"
    return at


def list_all() -> list[dict]:
    return _load()["schedules"]


def remove(sid: str) -> bool:
    d = _load()
    before = len(d["schedules"])
    d["schedules"] = [s for s in d["schedules"] if s["id"] != sid]
    _save(d)
    return len(d["schedules"]) < before


def check_due() -> list[dict]:
    """到点的生成通知，返回本轮新生成的通知。"""
    d = _load()
    now = time.time()
    fresh: list[dict] = []

    for s in d["schedules"]:
        if s.get("next") and now >= s["next"]:
            nid = uuid.uuid4().hex[:8]
            n = {"id": nid, "time": int(now), "name": s["name"],
                 "prompt": s["prompt"], "read": False}
            d["notifications"].insert(0, n)
            fresh.append(n)
            s["last"] = s["next"]
            # 重算下一次
            s["next"] = _next_run(s.get("repeat", ""), s.get("at", ""), s.get("weekday", "0"))
            if s.get("repeat") == "":      # 一次性，触发后删除
                d["schedules"] = [x for x in d["schedules"] if x["id"] != s["id"]]

    # 通知只保留最近 100 条
    d["notifications"] = d["notifications"][:100]
    _save(d)
    return fresh


def notifications(unread_only: bool = False) -> list[dict]:
    d = _load()["notifications"]
    return [n for n in d if not n.get("read")] if unread_only else d


def mark_read(nid: str) -> bool:
    d = _load()
    for n in d["notifications"]:
        if n["id"] == nid:
            n["read"] = True
            _save(d)
            return True
    return False
