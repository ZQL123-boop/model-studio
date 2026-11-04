"""界面访问密码（门禁）：远程打开页面要先输密码。

本机免密——物理接触已是最高权限，密码挡的是远程访问。
（原来的 lock.py 模型控制锁已移除：本机免密 + 控制权操作只能本机，锁没有存在意义。）

存 `data/gate.json`，sha256 哈希，不存明文。

安全边界：前端登录层挡的是"打开界面的人"；API 层另有 token 兜底。
防的是好奇/顺手打开，不是恶意攻击。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
GATE_FILE = DATA / "gate.json"


def _load() -> dict:
    if not GATE_FILE.exists():
        return {"password_hash": None}
    try:
        d = json.loads(GATE_FILE.read_text(encoding="utf-8"))
        return {"password_hash": d.get("password_hash")}
    except (OSError, json.JSONDecodeError):
        return {"password_hash": None}


def _save(d: dict) -> None:
    GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = GATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(GATE_FILE)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def has_password() -> bool:
    return bool(_load()["password_hash"])


def verify(pw: str) -> bool:
    h = _load()["password_hash"]
    return bool(h) and h == _hash(pw or "")


def set_password(pw: str, old_pw: str | None = None,
                 *, bypass: bool = False) -> tuple[bool, str]:
    pw = (pw or "").strip()
    if len(pw) < 4:
        return False, "密码至少 4 位"
    d = _load()
    if d["password_hash"] and not bypass and not verify(old_pw or ""):
        return False, "旧密码错误"
    d["password_hash"] = _hash(pw)
    _save(d)
    return True, "密码已设置"


def clear() -> None:
    """关闭门禁（清掉密码）。"""
    if GATE_FILE.exists():
        GATE_FILE.unlink()


def status() -> dict:
    return {"has_password": has_password()}
