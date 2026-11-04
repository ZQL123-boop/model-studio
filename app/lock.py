"""模型控制权锁定：给"别人用"的场景，防止被随意启停/切模型。

## 为什么需要

开放给别人（远程访问、给了 token）后，对方能聊天、查文件、用 agent。
但"启动/停止/切换模型"是控制权——启动会占 20 GB 内存、停止会打断
正在跑的任务、切换会改变所有人的体验。这些不该让普通使用者碰。

所以加一把锁：锁定时，任何启停/切换操作都要密码。

## 安全级别

这是"防止别人随手点"的锁，不是银行级鉴权：
- 密码 sha256 哈希存 `data/lock.json`，不存明文
- 但哈希是可以暴力破解的（弱密码秒破）
- 真正要防的是"不小心""好奇"，不是"恶意攻击"
恶意攻击得用更强的鉴权 + 限流，那是另一个话题。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
LOCK_FILE = DATA / "lock.json"


def _load() -> dict:
    if not LOCK_FILE.exists():
        return {"locked": False, "password_hash": None}
    try:
        d = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        return {"locked": bool(d.get("locked")),
                "password_hash": d.get("password_hash")}
    except (OSError, json.JSONDecodeError):
        return {"locked": False, "password_hash": None}


def _save(d: dict) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LOCK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LOCK_FILE)          # 原子替换，防止写一半损坏


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def is_locked() -> bool:
    return _load()["locked"]


def has_password() -> bool:
    return bool(_load()["password_hash"])


def verify(pw: str) -> bool:
    h = _load()["password_hash"]
    return bool(h) and h == _hash(pw or "")


def set_password(pw: str, old_pw: str | None = None,
                 *, bypass: bool = False) -> tuple[bool, str]:
    """设置或修改密码。已有密码时必须提供旧密码（本机 bypass 除外）。"""
    pw = (pw or "").strip()
    if len(pw) < 4:
        return False, "密码至少 4 位"
    d = _load()
    if d["password_hash"] and not bypass and not verify(old_pw or ""):
        return False, "旧密码错误"
    d["password_hash"] = _hash(pw)
    # 设密码即锁定：用户直觉是"设了密码就锁上了"。
    # 之前是"设密码"和"锁定"分两步，用户设完密码以为锁了，实际 locked=false，
    # 别人照样能启动/停止模型——被用户报告为"锁没用"。
    d["locked"] = True
    _save(d)
    return True, "密码已设置，模型已锁定"


def toggle(locked: bool, pw: str, *, bypass: bool = False) -> tuple[bool, str]:
    """锁定/解锁。bypass=True 时跳过密码校验（本机调用）。"""
    d = _load()
    if not d["password_hash"]:
        return False, "还没设置密码"
    if not bypass and not verify(pw or ""):
        return False, "密码错误"
    d["locked"] = bool(locked)
    _save(d)
    return True, "已锁定" if locked else "已解锁"


def status() -> dict:
    d = _load()
    return {"locked": d["locked"], "has_password": bool(d["password_hash"])}


# 环境变量里的主密码：即使忘了 data/lock.json 里的密码也能解锁。
# 可选，默认不设。
_MASTER = os.environ.get("MODEL_STUDIO_LOCK_MASTER", "").strip()


def check(pw: str) -> bool:
    """给 start/stop 用：密码正确，或者提供了主密码。"""
    if _MASTER and _hash(pw or "") == _hash(_MASTER):
        return True
    return verify(pw or "")
