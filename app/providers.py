"""云端 API provider。

和本地模型放在同一个列表里，但生命周期完全不同：
本地要起进程、等就绪、占 17 GB 内存；云端只是一个 base_url + key，选中即用。

key 的读取顺序（都不落盘、不回传前端）：
  1. 本项目 .env
  2. 环境变量
  3. ~/Projects/algo-rag/.env     ← 复用，那台机器上只有这一份 key

界面只显示"有没有 key"和"从哪来的"，永远不回传 key 本身。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()

# 内置预设。key_env 是去环境变量/.env 里找的键名。
PRESETS = [
    {"id": "api__deepseek", "name": "DeepSeek Chat", "base_url": "https://api.deepseek.com",
     "model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY", "note": "便宜、中文好"},
    {"id": "api__deepseek_r1", "name": "DeepSeek Reasoner", "base_url": "https://api.deepseek.com",
     "model": "deepseek-reasoner", "key_env": "DEEPSEEK_API_KEY", "note": "带推理链，慢"},
    {"id": "api__openai", "name": "OpenAI GPT-4o mini", "base_url": "https://api.openai.com/v1",
     "model": "gpt-4o-mini", "key_env": "OPENAI_API_KEY", "note": ""},
    {"id": "api__moonshot", "name": "Moonshot 8k", "base_url": "https://api.moonshot.cn/v1",
     "model": "moonshot-v1-8k", "key_env": "MOONSHOT_API_KEY", "note": ""},
    {"id": "api__dashscope", "name": "通义千问 Plus", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "model": "qwen-plus", "key_env": "DASHSCOPE_API_KEY", "note": "OpenAI 兼容模式"},
    {"id": "api__ollama", "name": "Ollama（本机服务）", "base_url": "http://127.0.0.1:11434/v1",
     "model": "qwen3:8b", "key_env": "", "note": "Ollama 自己管进程，需 ollama serve 在跑"},
]


def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _key_sources() -> list[tuple[str, dict[str, str]]]:
    """按优先级返回 (来源标签, 变量表)。"""
    out = [("本项目 .env", _read_env_file(ROOT / ".env")),
           ("环境变量", dict(os.environ))]
    rag_env = HOME / "Projects/algo-rag/.env"
    if rag_env.exists():
        out.append(("algo-rag/.env", _read_env_file(rag_env)))
    return out


_cache: dict[str, tuple[str, str]] = {}


def resolve_key(env_name: str) -> tuple[str, str]:
    """返回 (key, 来源)。没找到返回 ("", "")。"""
    if not env_name:
        return "", ""
    if env_name in _cache and _cache[env_name][0]:
        return _cache[env_name]
    for label, table in _key_sources():
        v = table.get(env_name, "")
        if v:
            _cache[env_name] = (v, label)
            return v, label
    return "", ""


@dataclass
class Provider:
    id: str
    name: str
    base_url: str
    model: str
    key_env: str = ""
    note: str = ""
    key_source: str = ""
    has_key: bool = False

    def to_public(self) -> dict:
        """给前端的版本——不含 key。"""
        return {
            "id": self.id, "name": self.name, "kind": "api",
            "base_url": self.base_url, "model": self.model,
            "note": self.note, "has_key": self.has_key,
            "key_source": self.key_source,
            # 同一套字段名，前端不用为云端写第二套渲染
            "size_gb": 0.0, "ready": self.has_key,
            "meta": {"remote": True, "model_name": self.model},
        }


def load_providers() -> list[Provider]:
    items: list[Provider] = []
    for p in PRESETS:
        key, src = resolve_key(p["key_env"]) if p["key_env"] else ("", "无需 key")
        note = p["note"]
        if p["key_env"] and not key:
            note = (note + " · " if note else "") + f"缺少 {p['key_env']}"
        items.append(Provider(
            id=p["id"], name=p["name"], base_url=p["base_url"], model=p["model"],
            key_env=p["key_env"], note=note,
            key_source=src, has_key=bool(key) or not p["key_env"],
        ))

    # 用户自定义：config/providers.json
    import json
    custom = ROOT / "config/providers.json"
    if custom.exists():
        try:
            for c in json.loads(custom.read_text(encoding="utf-8")):
                key, src = resolve_key(c.get("key_env", "")) if c.get("key_env") else ("", "无需 key")
                items.append(Provider(
                    id=c["id"], name=c.get("name", c["id"]),
                    base_url=c["base_url"], model=c["model"],
                    key_env=c.get("key_env", ""), note=c.get("note", ""),
                    key_source=src, has_key=bool(key) or not c.get("key_env"),
                ))
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    return items


def find(provider_id: str) -> Provider | None:
    for p in load_providers():
        if p.id == provider_id:
            return p
    return None


def auth_headers(p: Provider) -> dict[str, str]:
    key, _ = resolve_key(p.key_env) if p.key_env else ("", "")
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def mask(key: str) -> str:
    """只在日志里用。"""
    return f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***"
