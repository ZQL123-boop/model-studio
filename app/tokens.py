"""多 token 管理：给不同的人发不同的访问 token。

## 为什么

单 token 的痛点：所有人共用一个 token，无法区分谁在访问、
无法单独吊销某个人（换 token 所有人都要跟着改）。

方案：主 token（.env 里的 MODEL_STUDIO_TOKEN）+ 若干"附加 token"
（存 data/tokens.json，带备注和启用开关）。

- 给别人发一个附加 token，备注写"给小王"
- 小王不合作了 → 禁用或删除他的 token，其他人不受影响
- 主 token 永远是你自己的，别发给别人

## 安全

token 管理接口（增删改查）需要模型锁密码——这是控制权，不该让
普通使用者碰。token 本身还是明文存（哈希会破坏"复制给客户端"的用途）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
TOKENS_FILE = DATA / "tokens.json"


def _load() -> list[dict]:
    if not TOKENS_FILE.exists():
        return []
    try:
        d = json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
        return d.get("tokens", [])
    except (OSError, json.JSONDecodeError):
        return []


def _save(tokens: list[dict]) -> None:
    TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(TOKENS_FILE)


def master_token() -> str:
    return os.environ.get("MODEL_STUDIO_TOKEN", "").strip()


def verify(tok: str) -> bool:
    """校验 token：主 token 或任一启用的附加 token。"""
    tok = (tok or "").strip()
    if not tok:
        return False
    if tok == master_token():
        return True
    return any(t.get("token") == tok and t.get("enabled", True) for t in _load())


def list_tokens() -> list[dict]:
    """返回附加 token（脱敏：只显示前 8 位）。"""
    return [{"name": t.get("name", ""), "token": t.get("token", ""),
             "enabled": t.get("enabled", True),
             "preview": (t.get("token", "") or "")[:8] + "…"}
            for t in _load()]


def add(name: str) -> tuple[bool, str, str]:
    """添加一个 token，返回 (ok, msg, token)。"""
    name = (name or "").strip() or "未命名"
    import secrets
    tok = secrets.token_urlsafe(18)
    tokens = _load()
    tokens.append({"name": name, "token": tok, "enabled": True})
    _save(tokens)
    return True, f"已添加 {name}", tok


def remove(name_or_token: str) -> tuple[bool, str]:
    tokens = _load()
    before = len(tokens)
    tokens = [t for t in tokens
              if t.get("name") != name_or_token and t.get("token") != name_or_token]
    if len(tokens) == before:
        return False, "没找到这个 token"
    _save(tokens)
    return True, "已删除"


def toggle(name_or_token: str, enabled: bool) -> tuple[bool, str]:
    tokens = _load()
    hit = False
    for t in tokens:
        if t.get("name") == name_or_token or t.get("token") == name_or_token:
            t["enabled"] = bool(enabled)
            hit = True
    if not hit:
        return False, "没找到这个 token"
    _save(tokens)
    return True, "已启用" if enabled else "已禁用"
