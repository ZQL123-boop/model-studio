# MCP Server：把 Model Studio 的工具暴露给别的客户端

`mcp_server.py` 把 `app/tools.py` 里的 19 个工具包装成 [MCP](https://modelcontextprotocol.io) server，
Cursor / Claude Desktop 等客户端可以直接调用本机的文件、联网、画图、Office 工具。

## 客户端配置

**Cursor**（`~/.cursor/mcp.json`）

```json
{
  "mcpServers": {
    "model-studio": {
      "command": "/Users/ziqilin/Projects/model-studio/.venv/bin/python",
      "args": ["/Users/ziqilin/Projects/model-studio/mcp_server.py"]
    }
  }
}
```

**Claude Desktop**（`claude_desktop_config.json`）同上。

配好重启客户端，工具栏里应该出现 19 个工具。

## 暴露了哪些

| 类别 | 工具 |
|---|---|
| 文件读 | `read_file` `list_dir` `find_files` `grep_code` `analyze_data` |
| 文件写 | `write_file` `edit_file` |
| 联网 | `web_search` `fetch_url` `get_weather` `get_exchange_rate` |
| 生成 | `make_chart` `generate_image` `office` |
| 知识库 | `knowledge_search`（OI Wiki） |
| 界面检查 | `design` |
| 状态 | `list_goals` `set_goal` `check_task` |

**默认不暴露**：`run_bash`、`spawn_agents`——都能执行任意命令。MCP 客户端是被别的应用拉起的，等于把 shell 交给那个应用。真需要时：

```bash
MS_MCP_ALLOW_BASH=1 ./.venv/bin/python mcp_server.py
```

## 为什么动态生成函数

MCP 从**函数的类型签名**推断参数 schema。如果偷懒写：

```python
@mcp.tool()
def call_tool(name: str, arguments: dict) -> str:
    return tools.call(name, arguments)
```

客户端只会看到 `name` 和 `arguments` 两个参数，模型不知道该填什么，等于没法用。

所以 `_make()` 按每个工具的 JSON Schema 用 `exec` 生成显式签名：

```python
# read_file 的 schema → 生成
def read_file(path: str, max_lines: int | None = None,
              start: int | None = None, end: int | None = None):
    argv = {k: v for k, v in locals().items() if v is not None and v is not ...}
    return __CALL('read_file', argv)
```

这样客户端看到的参数名、类型、必填标记全对。

一个 Python 语法坑：**必填参数不能有默认值，可选参数必须有**，而 JSON Schema 的 `required` 不保证顺序。所以两组分开拼，required 在前。

## 验证记录

用标准 MCP stdio（JSON-RPC over stdin/stdout）实测：

```
1) initialize   OK  → serverInfo: {"name": "model-studio"}
2) tools/list   OK  → 19 个工具
   read_file 参数: ['path','max_lines','start','end']  必填: ['path']
3) tools/call list_dir  → 返回 app 目录 29 项（真实执行）
4) tools/call run_bash  → Unknown tool（默认不暴露，符合预期）
```

## 依赖

```bash
uv pip install --python .venv/bin/python mcp mcp-types cryptography
```

注意 `.venv` 是 uv 建的，没有 pip。沙箱环境里升级已有依赖会被拦（要删旧文件），
用 `--no-deps` 单独装能绕开——但 `mcp` 依赖 `mcp-types` 和 `cryptography`，得一起装。

## 相关

- `algo-rag/mcp_server.py`：另一个 MCP server，暴露算法知识库检索（3 个 tool）
- 区别：那个走 HTTP 转发（bge-m3 加载 100 秒，不能每次启动都加载）；
  这个直接 import `tools`（工具执行本来就是毫秒级，没有启动成本）
