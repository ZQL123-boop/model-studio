"""Model Studio 的工具暴露为 MCP server。

让 Cursor / Claude Desktop 等客户端直接调用本机的文件、联网、画图、Office 工具。
21 个工具，签名和参数直接从 `app/tools.py` 的 TOOLS schema 生成，不手写。

## 用法

    cd ~/Projects/model-studio
    ./.venv/bin/python mcp_server.py          # stdio 传输

## 客户端配置（Cursor: ~/.cursor/mcp.json）

{
  "mcpServers": {
    "model-studio": {
      "command": "/Users/ziqilin/Projects/model-studio/.venv/bin/python",
      "args": ["/Users/ziqilin/Projects/model-studio/mcp_server.py"]
    }
  }
}

## 安全

默认不暴露 `run_bash` / `spawn_agents`：MCP 客户端是被别的应用拉起的，
等于把 shell 交给那个应用。确实需要时设环境变量 `MS_MCP_ALLOW_BASH=1`。

## 为什么动态生成函数

MCP 从函数的类型签名推断参数 schema。如果用 `def f(**kwargs)` 包一层，
客户端看到的工具参数是空的，模型根本没法正确调用。所以按每个工具的
JSON Schema 用 exec 生成一个显式签名的函数（参数名、类型、必填都对）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import tools                                    # noqa: E402
from mcp.server.mcpserver import MCPServer               # noqa: E402

# 能执行任意命令的工具：默认不给
DANGEROUS = {"run_bash", "spawn_agents"}
ALLOW_DANGEROUS = os.environ.get("MS_MCP_ALLOW_BASH") == "1"

PY_TYPES = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _call(name: str, args: dict) -> str:
    """统一入口。tools.call 内部已把异常转成字符串返回。"""
    try:
        return tools.call(name, args)
    except Exception as e:                               # noqa: BLE001
        return f"[MCP 调用失败] {type(e).__name__}: {e}"


def _make(name: str, spec: dict):
    """按 JSON Schema 生成一个显式签名的函数。

    必填参数不能有默认值，可选参数必须有——Python 的语法限制，
    所以两组分开拼，required 在前。
    """
    params = spec.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])

    req_parts, opt_parts = [], []
    for k, v in props.items():
        py = PY_TYPES.get((v or {}).get("type"), "str")
        if k in required:
            req_parts.append(f"{k}: {py}")
        else:
            opt_parts.append(f"{k}: {py} | None = None")

    src = (
        f"def _f({', '.join(req_parts + opt_parts)}):\n"
        f"    argv = {{k: v for k, v in locals().items()\n"
        f"            if v is not None and v is not ...}}\n"
        f"    return __CALL({name!r}, argv)\n"
    )
    ns: dict = {"__CALL": _call}
    exec(compile(src, f"<tool:{name}>", "exec"), ns)
    fn = ns["_f"]
    fn.__name__ = name
    fn.__doc__ = spec.get("description") or ""
    return fn


mcp = MCPServer("model-studio")

_REGISTERED: list[str] = []
_SKIPPED: list[str] = []
for _item in tools.TOOLS:
    _f = _item.get("function") or {}
    _name = _f.get("name")
    if not _name:
        continue
    if _name in DANGEROUS and not ALLOW_DANGEROUS:
        _SKIPPED.append(_name)
        continue
    mcp.add_tool(_make(_name, _f), name=_name)
    _REGISTERED.append(_name)


if __name__ == "__main__":
    # stdout 是 MCP 协议通道，日志只能走 stderr，否则会污染 JSON-RPC
    msg = f"[model-studio MCP] 已注册 {len(_REGISTERED)} 个工具"
    if _SKIPPED:
        msg += f"（跳过 {', '.join(_SKIPPED)}，需要时设 MS_MCP_ALLOW_BASH=1）"
    print(msg, file=sys.stderr)
    mcp.run()          # 默认 stdio
