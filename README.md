# Model Studio

本地大模型的开关与调试台。一个 FastAPI 后端 + 单页前端，浏览器访问 `http://127.0.0.1:8600`。

**为什么是 web 而不是 SwiftUI**：后端必须能 `subprocess` 管模型进程、读内存、转发 SSE 流，
这些在 Python 里都是一行；用 Swift 得重写一遍 MLX 调用。界面用 web 技术能直接做流式光效，
打包成 `.app` 也有成熟路径（py2app / pywebview）。

**不是又一个 Chat UI**：它管的是模型进程本身——启动/停止、显存排查、工具调用循环、
上下文压缩，都是为「24 GB 内存跑 30B」这个硬约束服务的。

## 亮点

- **一套界面管三种后端**：本地 MLX / GGUF 进程、云端 API（DeepSeek / OpenAI / Moonshot / 通义）、Ollama
- **agent 模式**：20 个工具 + 工具路由器小模型（0.5B / 18 分类）+ 拒绝式回答自动打回
- **能自愈**：Metal OOM 后自动重启模型并重试；一键清理内存（实测释放 12.9 GB）
- **有真实调优数据**：`prefill-step-size=512` + `kv-bits 8`，把 4276 token 的长提示从「必崩」救到稳定
- **前端覆盖完整**：对话 / 知识库（RAG + 129 题回放）/ 记忆 / 设计 / 设置（门禁·锁·Token·主题色·写保护）

## 快速开始

```bash
bash run.sh                              # 起后端，打开 http://127.0.0.1:8600
cd web && npm install && npm run dev     # 只有改前端时才需要
```

左侧选模型 → 启动（首次 10~30 秒加载权重）→ 开始对话。

停止服务：

```bash
lsof -nP -iTCP:8600 -sTCP:LISTEN -t | xargs kill
```

## 架构

```text
浏览器 :8600
  └─ FastAPI (app/)
       ├─ /                 React 产物（web/dist）
       ├─ /api/start|stop   模型进程生命周期（runner.py）
       ├─ /api/chat|agent   SSE 流式对话 / 工具循环（main.py + agent.py）
       ├─ /api/rag/*        转发到 algo-rag serve.py（:8601）
       └─ /v1/*             OpenAI 兼容网关
            │
            ├─ mlx_lm.server :8080   本地模型（30B，16 GB 权重）
            └─ algo-rag      :8601   RAG（bge-m3 + qdrant）
```

## 目录

- [功能](#功能)
- [前端（React + Vite）](#前端react--vite-web)
- [Agent 接入（重点）](#agent-接入重点)
- [一键启动](#一键启动)
- [代码结构与接口](#代码结构与接口)
- [四个关键设计决策](#四个关键设计决策)
- [已知限制](#已知限制)
- [排障记录（2026-11-02）](#排障记录2026-11-02)
- [还能加的](#还能加的)

---

## 功能

| 模块 | 已实现 |
|---|---|
| 本地模型发现 | 扫 `~/Projects/*/models/*`、`~/models/*`、HF cache；识别 **MLX / GGUF（目录或单文件）**；自动排除 embedding、reranker |
| **云端 API** | 内置 DeepSeek / OpenAI / Moonshot / 通义 / Ollama；选中即用，不用起进程；支持自定义 provider |
| 生命周期 | 启动 / 停止 / 重启；**一键清理内存**（停模型 + 杀残留 mlx_lm.server 孤儿，报告释放 GB）；状态机 `stopped → starting → ready / error` |
| 对话调试 | 流式输出、多轮历史、清空会话 |
| 参数 | temperature / top_p / max_tokens 实时滑杆；thinking 开关（需重启生效） |
| 监控 | 系统内存（含模型）、进程 RSS、运行时长、端口、CPU |
| 日志 | 模型服务 stdout 实时滚动 |
| 指标 | 首 token 延迟、总耗时、块/s |

## 前端（React + Vite，`web/`）

### 界面

| 对话（agent 自动调工具） | 知识库（检索 + 出处） |
|---|---|
| ![对话](docs/screenshots/01-chat.png) | ![知识库](docs/screenshots/02-rag.png) |

| 回放 129 题（RAGAS 分数） | 记忆（长期记忆 + 历史） |
|---|---|
| ![回放](docs/screenshots/03-replay.png) | ![记忆](docs/screenshots/04-memory.png) |

| 设计（预览 + 截图 + 布局检测） | 设置（门禁 / 锁 / Token / 主题色 / 写保护） |
|---|---|
| ![设计](docs/screenshots/05-design.png) | ![设置](docs/screenshots/06-settings.png) |

> 截图脚本：`scripts/shot_ui.py`（Playwright，自动登录 + 发请求 + 截图）。
> 若 Chromium 未安装：`python -m playwright install chromium`；
> 国内网络若卡在 CDN，可设 `PLAYWRIGHT_DOWNLOAD_HOST=<镜像>`，或手动截图按上面的文件名放进去。

```text
web/
├── src/
│   ├── App.tsx              布局 + 状态轮询 + toast
│   ├── api.ts               fetch / SSE 封装（含流式逐帧解析）
│   ├── types.ts             后端返回结构
│   ├── style.css            深色主题
│   └── components/
│       ├── ModelList.tsx    模型列表 + 启动/停止 + 一键清理内存
│       ├── ChatPanel.tsx    流式对话 + agent 工具步骤 + 自动选型标签
│       ├── StatusPanel.tsx  内存 / RSS / 端口
│       └── LogView.tsx      模型日志轮询
```

开发（热更新，`/api` 代理到 8600，同源不用配 CORS）：

```bash
cd web && npm install && npm run dev    # http://127.0.0.1:5173
```

上线：`cd web && npm run build` → 产物在 `web/dist/`。后端启动时自动检测：

- **有 dist** → `/` 返回 React 产物，`/assets` 挂静态文件（**不需要再跑 Vite**，直接开 `127.0.0.1:8600`）
- **没 dist** → 回退旧单页 `static/app.html`，保证永远能打开

> 旧版单页 `static/app.html` 仍保留（含知识库/记忆/设计/锁/Token 等低频面板）。
> React 版先覆盖核心路径（模型启停 + 对话 + 状态 + 日志），其余面板后续逐个迁移。

## Agent 接入（重点）

Model Studio 对外暴露一个 **OpenAI 兼容端点**，Agent 只需要知道这一个地址：

```
base_url = http://127.0.0.1:8600/v1
api_key  = 不填也行（除非设了 MODEL_STUDIO_TOKEN）
```

`model` 字段决定走哪：

| model | 去向 |
|---|---|
| `local` / `default` / `Qwen3-30B-A3B-4bit` | 本机 MLX 进程，**没启动会自动拉起** |
| `deepseek-chat` / `deepseek-reasoner` | DeepSeek 云端 |
| `qwen-plus` / `gpt-4o-mini` / `moonshot-v1-8k` | 对应云端（有 key 才出现） |

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8600/v1", api_key="not-needed")

# 本地模型——进程没起会自动拉起
r = client.chat.completions.create(
    model="local",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

LangChain / LlamaIndex / AutoGen 都直接填这个 `base_url`，不用改别的。

### 三个必须知道的行为

**1. 首次请求会等 20~45 秒。** 本地模型没启动时，网关会先拉起它（加载 11 秒 + Metal 编译 11 秒）。
**Agent 的 timeout 必须 ≥ 120 秒**，否则第一次调用一定超时。之后的请求 1~2 秒。

**2. 流式原样透传。** 上游 SSE 帧不做任何改写，`[DONE]` 也原样转发。Agent 端按标准 OpenAI 格式解析即可。

**3. 进程会自动被接管。** studio 重启后，之前拉起的模型进程还活着（占 17 GB）。
`runner` 会通过 `lsof` 找到它并接管，界面显示「运行中（接管）」，可以直接停止。
不接管的话就是：界面显示"未启动"、点启动撞端口、17 GB 再也释放不掉。

实测的重启接管：`state=ready pid=1546 adopted=True`，
停止后 0.4 秒释放、内存从 21.5 GB 掉到 5.3 GB。

### 鉴权

默认**不校验**，靠绑定 `127.0.0.1` 保护。要开鉴权：

```bash
export MODEL_STUDIO_TOKEN=your-token     # 或在 .env 里
```

之后所有 `/v1/*` 请求必须带 `Authorization: Bearer your-token`，界面右侧会显示"Bearer token 已开启"。

**别把 8600 绑到 0.0.0.0 又不设 token**——那就是给局域网开了个免费 30B 模型。












## 模型控制锁（v0.13）

右上角「🔒」按钮。给"别人用"的场景：对方能聊天、查文件、用 agent，
但**启动/停止/切换模型需要密码**。

### 三种状态

| 按钮 | 状态 | 含义 |
|---|---|---|
| 🔒 设密码 | 还没设密码 | 点一下设置（至少 4 位） |
| 🔓 已解锁 | 有密码但没锁 | 启停模型无需密码 |
| 🔒 已锁定 | 已锁定 | **启停模型需要密码** |

### 后端强制，不是前端挡

锁定后 `/api/start`、`/api/stop` 直接在**后端**校验密码，无密码或错密码返回 403。
前端那个锁按钮只是 UI，直接 curl 也一样被拦。

### 密码存储

- `data/lock.json`：sha256 哈希，不存明文
- 可选环境变量 `MODEL_STUDIO_LOCK_MASTER`：主密码，忘了锁密码时也能解锁

### 安全边界（如实说）

这是"防止别人随手点/好奇"的锁，**不是银行级鉴权**。sha256 哈希对弱密码
可以秒级暴力破解。要防恶意攻击得加限流 + 强鉴权，那是另一件事。

### 使用场景

```
你（主人）：设密码 → 锁定 → 把链接和 token 给别人
别人：聊天 ✓ 查文件 ✓ 用 agent ✓ 启动/停止模型 ✗（要密码）
```

## 云端模型接入（v0.12）

对话框上方加了模型选择器：

```
本地模型 | DeepSeek Chat（快） | DeepSeek Reasoner（强但慢）
```

选云端后，同一套 agent 逻辑、同一套工具，但模型换成 DeepSeek。

### 为什么这一步是关键

前面所有 C 类限制（多文件、10 步、改对代码）**不是代码问题，是 30B 的能力上限**。
换成云端后，同样的 `/api/agent/stream` 立刻放大一个量级。

### 实测对比

| 任务 | 本地 30B | DeepSeek Chat |
|---|---|---|
| 写 fib.py 并验证 | 12.1s | **2.3s** |
| 统计 app 下函数数 | 会 OOM | **6.3s，4 轮** |

更关键的是**能力质变**。统计函数数这个任务，云端模型没有机械地 `read_file` 每个文件，
而是：

```
🔧 run_bash: find *.py | head
🔧 run_bash: python3 -c '用 ast 统计所有文件函数数'
```

**它会写脚本解决问题**，而不是一个个读。本地 30B 只会 read_file + 卡住。

而且答案质量更高：

> 函数最多的是 main.py（34 个）。补充：按行数排第一名是 tools.py（825 行），
> 但它只有 20 个函数，说明单个函数平均比 main.py 长。

**主动补充了第二种排序口径**——30B 做不到这种分析深度。

### 成本

DeepSeek Chat 的 key 在 `algo-rag/.env` 里（复用）。按量计费，agent 任务一次约 0.01-0.05 元。

## B 类补齐（v0.11）

之前判断"不划算"的五项，按实用性重排后全做了。

### 1. 联网：web_search + fetch_url

**`web_search(query)`**：Bing 抓取 + 相关性过滤（标题/摘要必须含查询词）。

实测 `web_search("FastAPI 最新版本")` → 0.4s，返回官方文档/菜鸟教程，相关性好。

**局限**：生僻技术词（"树状数组"）Bing 断词差会返回"树木"。搜这类还是用模型自己的知识。

**`fetch_url(url)`**：readability-lxml 提取正文（去导航/广告/侧栏）。

实测抓 `fastapi.tiangolo.com` → 1.1s，提取出干净正文。JS 渲染的页面抓不到（返回"提取不到正文"）。

### 2. 任务取消

流里先推 `{"id": "e8f1aa7d"}`，前端拿这个 id 调 `/api/agent/cancel`。

```
任务 id: e8f1aa7d → 发送取消 → {'ok': True} → 任务最终「已取消」
```

取消检查点：每轮开头 + 上游流的每个 chunk 之间。`background=_cleanup` 在流结束后清理注册表。

### 3. 写保护（替代人工确认弹窗）

弹窗式确认在 SSE 里实现复杂且打断节奏。改成**开关式写保护**：

| 状态 | 覆盖已存在文件 |
|---|---|
| 写保护关 | ✅ 覆盖（自动备份） |
| **写保护开** | ❌ 拒绝，模型会改新文件名 |

前端「写保护」复选框，切换调 `/api/tools/write-protect`。追加（append）不受影响。

### 4. 会话存档

任务 done 时自动存 `data/agent_sessions/<id>.json`：

```json
{"id": "...", "goal": "...", "plan": [...], "changed": [...],
 "turns": 2, "elapsed_s": 12.5, "steps": [...]}
```

`/api/agent/sessions` 列出最近的会话（能回看改了什么、当时怎么规划）。**不能续跑**——那要存完整 messages，价值/成本不划算。

### 验证

| 项 | 结果 |
|---|---|
| web_search | ✅ 模型主动搜，2 轮 12.5s |
| fetch_url | ✅ 1.1s 提取干净正文 |
| 任务取消 | ✅ ok:true → 任务已取消 |
| 写保护 | ✅ 覆盖拒绝 / 新建放行 / 追加放行 |
| 会话存档 | ✅ 1 个会话，含 goal/plan/changed |

## A 类优化（v0.10）

三项性能/体验优化，做完 agent 的交互就完整了。

### 1. 工具结果缓存

同一个任务里，只读工具的相同调用直接命中缓存。

```
第一次 read_file(tools.py, 20行)  →  0.004s
第二次同样调用                    →  0.000s（"缓存命中，未重新读取"）
```

只缓存 `read_file` / `read_files` / `list_dir` / `find_files`。
写操作不缓存——文件内容会变。

**为什么有用**：模型读文件时经常重复（先看结构、再读细节时手滑重读全文）。

### 2. 并行工具调用

一轮里模型同时要读多个文件时，只读工具用 `asyncio.gather` 并行跑。

```python
read_items = [tc for tc in calls if name in READ_ONLY_TOOLS]
if len(read_items) > 1:
    done = await asyncio.gather(*[
        asyncio.to_thread(tools.call, nm, ar) for ...
    ])
```

**只并行只读**——写操作必须顺序（后一个可能要读前一个的结果）。
并行时不改 `state`（有竞争），跑完用 `agent.record()` 补记。

### 3. 流式答案

原先每轮非流式调上游，用户要干等 20-40 秒才看到任何东西。现在改成流式：

```
⏱ 流式片段 #1: '脚'
⏱ 流式片段 #2: '本'
⏱ 流式片段 #3: '成功'
```

**关键难点**：流式的 `tool_calls` 是分块下发的，要按 `index` 累积拼装：

```python
for tc in delta.get("tool_calls") or []:
    slot = tool_acc.setdefault(tc["index"], {"id":"", "name":"", "arguments":""})
    if tc.get("id"): slot["id"] = tc["id"]
    slot["name"] += fn.get("name") or ""
    slot["arguments"] += fn.get("arguments") or ""
```

拼错了整个 agent 就废了（工具调不起来）。实测拼装正确。

### 实测（三项一起）

```
问：写 stream_test.py 打印 1 到 5 的和，跑一下

🔧 write_file → 🔧 run_bash (15) → 流式输出「脚本成功运行，输出结果为 15」
14.1 秒，4 轮，18 个流式片段
```

## Agent 化（v0.9）

从"带工具调用的聊天"升级成真正的 agent 循环。核心在 `app/agent.py`（约 260 行）。

### 工具从 6 个扩到 7 个

新增 **`run_bash`**——这是最关键的一个。没有执行能力，agent 再会规划也只能看和写，不能验证。

| 工具 | 作用 |
|---|---|
| `read_file` / `list_dir` / `find_files` | 读 |
| `write_file` / `make_dir` / `move_file` | 写（sandbox + Desktop，自动备份） |
| **`run_bash`** | **执行命令**（30 秒超时，输出截断 4000 字符） |

`run_bash` 的安全设计：

```
工作目录固定 sandbox
危险命令正则拦截：rm -rf /、sudo、curl|sh、mkfs、dd、fork炸弹、关机重启
PATH 最前插入 sandbox/.bin（里面是 python → python3 的 wrapper）
```

**为什么加 python wrapper**：macOS 没有 `python` 命令，模型写 `python xxx.py` 会
`command not found`，然后它以为"沙箱没装 Python"，转头问用户要不要安装。

### 七项 agent 能力

| 能力 | 实现 |
|---|---|
| **任务规划** | `write_plan` 工具，3-6 步，每轮注入 system |
| **进度跟踪** | `AgentState.cursor`，按 cursor 判断重复（不看文本，模型措辞每次都变） |
| **卡住检测** | 连续 3 次相同 `(tool, args)` 签名 → 中断 |
| **失败重试** | 连续 2 次失败 → 工具结果里追加"必须换方法" |
| **上下文压缩** | 超 9000 字符 → 把早期工具结果截到 200 字符，保留最近 4 个完整 |
| **完成判断** | 计划没走完 → answer 末尾标"还有 N 步没做" |
| **改动追踪** | done 事件带 `changed: ['fib.py']` |

### 上下文压缩是抗 OOM 的关键

30B 占 20 GB，每轮把工具结果全文塞回上下文，4 轮就撑爆 Metal（
`kIOGPUCommandBufferCallbackErrorOutOfMemory`）。

压缩实测：13332 → 7824 字符（省 41%），最近 4 个结果保持完整。

**这不解决模型能力，但能让它多跑几轮。**

### 端到端验证

```
问：写一个 fib.py 计算斐波那契第 20 项，跑一下验证结果

🔧 write_file: fib.py
🔧 run_bash: python fib.py  →  [exit 0] 6765
■ [DONE]

12.1 秒，3 轮，改动 ['fib.py']
答：斐波那契第 20 项的计算结果是 6765。
```

**这是真正的 agent 行为**：写代码 → 执行 → 读结果 → 报结论，而不是"我写了一个文件"。



### 多文件任务：结构摘要（v0.9.2）

30B 只有 4 GB 留给上下文，读 3 个 400 行的文件就满了。所以加了两个工具：

**`read_files(paths[])`**：一次看多个文件的**结构**，不读全文。

```
agent.py（220 行, 8722 字节）
  函数: all_tools(1参)@97, execute(4参)@101, _msg_len(1参)@146, ...
  类: AgentState(2方法)@34
  导入: __future__, json, dataclasses
```

用 Python `ast` 解析，提取函数名/参数个数/行号、类名/方法数、导入。
**3 个文件（含 657 行的 tools.py）压成约 500 字符，全文是 38000 字符——省 98%。**

**`read_file(path, start, end)`**：按行范围读。先看摘要定位到第 40 行，
再只读 40-80 行，而不是整个文件塞进去。

### 实测：4 轮分析 8 个文件

```
问：读 app 下 py 文件结构，说明分成哪几个模块

🔧 list_dir
🔧 read_files: [agent.py, gateway.py, main.py, memory.py, models.py, ...]
🔧 read_files: [...]
答：6 个模块：Agent / 网关 / 主程序 / 记忆 / 模型 / 其他

37.8 秒，4 轮，没 OOM
```

改造前读 3 个文件就撑爆，现在能看 8 个。

### Linux 路径纠正

模型（Qwen3）训练语料里大量 `/home/user`、`/root`，它生成路径时先验就是这个。
实测它调 `read_files(["/home/user/Projects/..."])` → 路径不存在 → 任务失败。

两道防线：

1. **prompt 里明确说**：「系统是 macOS，家目录是 `/Users/ziqilin`，
   **不是 /home/user，也不是 /root**」
2. **`_fix_linux_path()` 兜底**：把 `/home/user`、`/root`、`/home/*` 自动改写成 `~/`

改完模型就用对路径了。

### sandbox 独立 venv（v0.9.1）

`pip install` 装到的是 **`~/Projects/sandbox/.venv`**，不是系统 Python。

| 项 | 值 |
|---|---|
| 创建时机 | 首次 `run_bash` 时惰性创建（1.7 秒） |
| 复用 | 之后 0.01 秒，不重建 |
| 隔离效果 | 实测装 8 个包，系统 `python3` 一个都 import 不到 |

**为什么必须隔离**：不隔离的话模型装十个包，你的系统 Python 就脏了，
版本冲突还查不出来。sandbox 里的 venv 删掉就干净了。

### pip 超时自动放宽到 150 秒

`run_bash` 默认 30 秒超时，但 `pip install` 会自动给 150 秒。

**为什么**：实测 `pip install pyshorteners` 要 118 秒——它有 6 个传递依赖
（requests/urllib3/certifi/charset-normalizer/idna/six）。30 秒必然超时，
模型会以为"装不上"，然后放弃。

也可以显式传 `timeout`（最大 300）：

```json
{"command": "pip install torch", "timeout": 300}
```

### 一个已知的遗留

之前测试时用 `pip3 install six` 直接装到了系统 Python。那个 `six` 还在系统里，
和 venv 无关。想清掉：`pip3 uninstall six`。

### 没做的：人工确认

危险操作的事中确认（暂停等用户点同意）在 SSE 流里实现复杂。
现在的替代方案：**写文件自动备份 + done 时列出改动清单**，可回滚、可追溯。

## 文件工具（v0.8）

对话页输入框上方有「允许读文件」开关。勾上后走 `/api/agent/stream`，
模型可以自己决定读哪个文件、列哪个目录，最多 4 轮工具调用。

### 三个只读工具

| 工具 | 作用 | 限制 |
|---|---|---|
| `read_file` | 读文本文件 | 单文件 100 KB、最多 400 行 |
| `list_dir` | 列目录 | 最多 200 项，跳过隐藏文件 |
| `find_files` | 按 glob 找文件 | 最多 40 个，不递归超 4 层，跳过 node_modules/.venv |

### 写操作：单独一个根 + 自动备份

**读**：`~/Projects` 全范围（`MS_FILE_ROOT` 可改）
**写**：只允许 `~/Projects/sandbox`（`MS_WRITE_ROOT` 可改）

为什么分开：读错只是答得不对，你能看出来；写错可能把项目代码覆盖掉。
sandbox 是独立目录，写坏了不影响任何真实项目。

| 工具 | 行为 |
|---|---|
| `write_file` | 创建/覆盖。**覆盖前自动备份到 `.ms_backup/<时间戳>/`** |
| `make_dir` | 建目录（含父目录） |
| `move_file` | 移动/重命名，目标已存在也先备份 |

**不提供删除**。误删没法撤销，而删除的需求可以用 move_file 挪进 `_trash/` 代替。

备份可直接回滚：

```bash
ls ~/Projects/sandbox/.ms_backup/          # 按时间戳分目录
cp ~/Projects/sandbox/.ms_backup/20261030-181537/test/round.py \
   ~/Projects/sandbox/test/round.py        # 还原
```

### 读写路径基准一致

相对路径先按 `~/Projects` 找，不存在再按 `~/Projects/sandbox` 找。
不这么做的话模型刚 `write_file("a.py")` 写进 sandbox，
接着 `read_file("a.py")` 会在 `~/Projects/a.py` 找不到。

### 多轮连续性

agent 通道**必须带对话历史**。第一版没带，问「把刚才那个文件改成…」时模型
既不知道改哪个文件，还编了个 `range(1, 101)` 出来。带上历史后：

```
问：创建 count.py，打印 1 到 10        → write_file(count.py, 1..11)  6.3s
问：把那个文件改成打印 1 到 30          → write_file(count.py, 1..31)  6.8s  ✓
```

### 沙箱（默认 `~/Projects`，可用 `MS_FILE_ROOT` 改）

实测拦截：

```
list_dir(~/Projects/model-studio)          → ✅ 列出 11 项
read_file(README.md, max_lines=5)          → ✅ 读出并截断
read_file(/etc/passwd)                     → ❌ 越界：只允许访问 ~/Projects 下
read_file(~/Projects/model-studio/.env)    → ❌ .env 是敏感文件
read_file(../../etc/hosts)                 → ❌ 越界（resolve 后折叠成绝对路径再校验）
read_file(static/icon-180.png)             → ❌ .png 不是文本文件
```

路径校验用 `Path.resolve()` + `is_relative_to(ALLOW_ROOT)`——
`../` 会被折叠成绝对路径，然后被拦住。

**敏感文件白名单**：`.env` / 私钥 / `.pem` / `credentials` / `.ssh` 等，
即使在沙箱内也不给读。模型读到 API key 可能写进回答里，那就泄漏了。

### 实测（本地 30B-A3B）

| 问 | 工具调用 | 耗时 | 结果 |
|---|---|---|---|
| 看下 algo-rag 目录有什么 | `list_dir` × 1 | 8.4s | 正确列出 configs/data/docs |
| 读 README 第一行 | `read_file` × 1 | 4.6s | 答「Model Studio」 |
| 树状数组是什么 | **0 次** | 5.5s | 直接回答，没瞎调工具 |

模型能正确判断**什么时候该用工具、什么时候不该**——这是 agent 可靠性的关键。

## 能力边界（v0.7）

本地模型的通病：被问到做不到的事（查天气、读文件、联网），倾向于**编一个
看起来合理的答案**。对用户来说这比报错更糟——你不知道哪句是真的。

`app/persona.py` 里写死了 `BOUNDARY`，注入 system prompt 最前面（约 150 token/轮）。

### 为什么不是一句话

"不知道就说不知道"这种抽象指令模型会忽略。有效的是**逐条列具体不能做的事**，
并规定回答格式：

```
1. 第一句直接说做不到，不要铺垫
2. 一句说明原因（没有联网 / 没有文件权限 / 知识有截止日期）
3. 如果能给替代方案就给
```

### 双向实测

| 问题 | 结果 | 耗时 |
|---|---|---|
| 现在天气怎么样？ | 「我没法获取实时天气信息——我没有联网能力。你可以用天气应用查询」 | 2.9s |
| 帮我看看 /etc/hosts 写了什么 | 「我没法读取文件内容——我没有文件访问权限。你可以用 cat /etc/hosts」 | 2.0s |
| 2027 年世界杯谁夺冠 | 「我无法提供2027年世界杯的冠军信息——我没有联网能力」 | 2.1s |
| 树状数组的 lowbit 是什么 | 正常回答（举例 6→2） | 3.5s |
| 用 Python 写二分查找 | 正常给出代码 | 4.7s |
| 快速幂的原理 | 正常解释（分解二进制位） | 6.0s |

**前三个拒答，后三个正常**——没有过度拒答。

### 注入顺序

```
system = 能力边界（约束怎么回答）
       + 本机状态（回答时可参考的事实）
       + 手写记忆（长期偏好）
```

边界放最前，因为它管的是"行为"；后面两段是"素材"。

## 记忆（v0.6）

顶部第三个标签「记忆」。之前的对话**只存在浏览器内存里，刷新就没**，
日志里连请求体都没记——聊过的东西一条都找不回来。现在落盘了。

### 存在哪

| 文件 | 内容 |
|---|---|
| `data/chat_history.jsonl` | 每条一行：时间、模型、问题、答案、耗时、TTFT、token 数 |
| `data/memory.json` | 设置：开关 / 深度 / 上限 |

**不存 thinking 过程**。推理模型的思考链动辄上千 token，存下来文件膨胀十倍，
而回看时几乎没人读思考。


### 两种记忆，别混

「记忆」标签里有**两块**，作用完全不同：

| | 长期记忆（手写） | 历史记录（自动） |
|---|---|---|
| 来源 | 你自己写 | 每次对话自动追加 |
| 内容 | 事实、偏好、身份 | 一问一答流水 |
| 注入方式 | 作为 **system** 前置 | 按 depth 拼进 messages |
| 存储 | `data/notes.md` | `data/chat_history.jsonl` |
| 上限 | 2000 字符 | 1000 条（可调） |

**长期记忆**是 WorkBuddy 里 `MEMORY.md` 那种东西——写“我叫什么、回答要直接、我在做 RAG 项目”。
每次对话都会作为 system prompt 的一部分，模型始终遵守。

**历史记录**是流水账，靠 `depth` 控制带最近几轮。写多了反而挤爆上下文。

### 长期记忆的实现

```python
# memory.py
def notes_as_system() -> str | None:
    t = load_notes()["text"].strip()
    if not t:
        return None
    return "以下是用户写的长期记忆，回答时要始终遵循：\n```\n" + t + "\n```"
```

注入点在 `_payload()`，**只走 `/api/chat*`**。`/v1` 是给外部 agent 的，
那边有自己的 system prompt，不能污染。

超过 2000 字符直接截断（不是报错）——写的人已经花时间了，别让他白写。

实测：写入“回答必须加 [BOSS]”，问“1+1”，回答是 `1+1 等于 2。[BOSS]`。

### 三个设置

| 设置 | 作用 | 默认 |
|---|---|---|
| **启用记忆** | 是否把历史带给模型。关掉仍然记录，只是不注入上下文 | 开 |
| **上下文深度** | 每次发送带最近 N 轮（一问一答算一轮）。0 = 完全不带 | 6 |
| **磁盘上限** | 超过自动删最旧的 | 1000 |

深度这一项很关键：`state.messages` 会一直增长，全量发给模型迟早撑爆 8k 窗口。
现在 `send()` 里只取 `slice(-depth * 2)`。

### 顺手修的：对话接口不再甩 409

之前模型没启动时 `/api/chat/stream` 直接返回
`409 本地模型未就绪，先启动`，用户得自己去点启动按钮。

现在会自动拉起（`ensure_local_ready`），冷启动 20~40 秒，
客户端等这一下就好。实测：stopped 状态下发一句话，18.7 秒完成并落盘。

### 接口

```
GET    /api/memory              设置 + 条数 + 文件大小 + 路径
POST   /api/memory              改设置
GET    /api/memory/history      最近 N 条（新的在前）
GET    /api/memory/context      最近 depth 轮，转成 messages
DELETE /api/memory/history      清空
```

## 一键启动

双击桌面 **`ModelStudio.command`**。

它做四件事：

1. 检测 8601（知识库）和 8600（界面）是否已在跑，只在缺的时候启动
2. 等界面就绪再打开浏览器
3. 播报知识库加载进度（bge-m3 要 40 秒）
4. **前台保持运行**——关掉终端窗口或 Ctrl+C 就停止服务

不想要窗口常驻的话，两个服务各自 `nohup` 起来也行，脚本只是把这些串起来。

## 回放：129 道预跑题

知识库标签里有「在线提问 / 回放 129 题」两个子页。回放模式读的是已经跑好的产物，
**不调模型**，所以秒开、断网也能用：

| 数据 | 来源 |
|---|---|
| 答案 / 引用 / 延迟 | `gen_answers_v4.jsonl` |
| RAGAS 分数 | `gen_eval_v4.json` |
| 检索片段 / gold_spans | `gen_context_v4.jsonl` |

每道题能看到：答案（引用高亮）、faithfulness、answer_relevancy、生成耗时、
检索到的 5 段原文（可展开）、人工标注的标准答案。

**负样本有明确判定**：`answerable=false` 的题会标「已正确拒答」或
「应拒答但答了」——后者就是评测里要重点讲的失败案例。

## 融合 RAG（v0.5）

顶部有「对话」「知识库」两个标签。知识库那页就是原来的 algo-rag Streamlit 界面，
功能等价：

- 提问 → hybrid 检索 → 可选重排 → 本地模型生成
- 答案带出处标注，检索片段可展开对照原文
- 显示检索/生成耗时、余弦相似度、拒答判定

后端多了一层代理：`app/rag.py` 把 `/api/rag/*` 转发到 algo-rag 的 `serve.py:8601`。

**为什么不在这个进程里直接跑 RAG**：venv 里没有 torch/faiss/qdrant，
Searcher 加载要 40 秒且 qdrant 本地模式不支持并发访问。

启动顺序：

```bash
cd ~/Projects/algo-rag && ./.venv/bin/python serve.py 8601   # 先起 RAG
cd ~/Projects/model-studio && bash run.sh 8600 0.0.0.0        # 再起界面
```

RAG 没起来时，「知识库」标签会显示「未启动」，不影响对话功能。

## 界面（v0.4）

> **已过时**：界面已重写为 React（见上面「前端（React + Vite）」章节），
> 本节保留当时原生 JS 版的布局设计记录。

右侧监控区加了三条实时曲线（Canvas 手绘，无第三方图表库）：

| 曲线 | 数据源 | 窗口 |
|---|---|---|
| 内存 | 系统已用 / 总量 | 2 分钟（60 点 × 2s） |
| CPU | 系统整体 + 进程占用 | 同上 |
| 生成速度 | 最近一次对话的块/s | 同上 |

**CPU 采样有个坑**：`psutil.Process.cpu_percent()` 是「两次调用之间的增量」，
必须复用同一个 Process 对象。每次 new 一个会永远返回 0.0，曲线变平线。
`runner.py` 里用模块级 `_cpu_proc` 缓存解决。

**左侧只显示本地模型**（MLX / GGUF / Ollama）。云端 provider 仍保留在
`providers.py` 里，通过 `/v1` 用 `model=deepseek-chat` 依然能调——
只是管理界面不再展示，避免误点。

## 当前装的三个模型

| 模型 | 格式 | 大小 | 速度 | 用途 |
|---|---|---|---|---|
| Qwen3-30B-A3B-4bit | MLX | 16.0 GB | **33 tok/s** | 日常主力（MoE，快） |
| Qwen3.8-27B-4bit | MLX | 15.0 GB | 5.5 tok/s | 高质量短问答 |
| Qwen3.8-27B-UD-IQ4_XS | GGUF | 13.3 GB | 5.0 tok/s | 同上，llama.cpp 路线 |
| DeepSeek-R1-Distill-Qwen-14B | MLX | 7.8 GB | **10.7 tok/s** | 推理任务（思考链长） |

GGUF 走 `~/Projects/qwen38-local/llama-b11963/llama-server`（通过 `~/.local/bin/llama-server` 软链暴露）。
启动参数里三个不能省的：

- **`--jinja`**：Qwen3.8 用自定义 chat template，缺了输出乱码
- **`--reasoning off`**：thinking 默认开启，不关的话 16 个 token 全花在思考上、正文一个字出不来
- **`--cache-type-k/v q8_0`**：32K 上下文 KV cache 从 2.3 GB 降到 1.3 GB

## 推理模型的支持（DeepSeek-R1 等）

R1 这类模型把思考过程放在 **`reasoning`** 字段，正文在 `content`。两个后果：

**1. 只读 `content` 会看到空白。** `max_tokens` 不够时思考还没结束，`content` 直接是 `null`。
实测 1+1 要 438 token，其中 400+ 在 reasoning 里。

**2. 界面要单独显示思考过程。** 现在 `Model Studio` 的对话区会渲染一个可折叠的「思考过程」区块，
数据来自 SSE 帧的 `r` 字段。

字段名有两个变体，都兼容：
- mlx_lm.server → `delta.reasoning`（**增量**，每帧几个字符）
- DeepSeek 云端 → `delta.reasoning_content`（**也是增量**）

前端用前缀判断区分「累积全文」和「增量」，追加即可。

## 本地 vs 云端

同一个问题走两条链路，实测对比（Mac mini M2 / 24GB）：

| | 本地 Qwen3-30B-A3B | DeepSeek Chat |
|---|---|---|
| 首 token | **0.52 s** | **0.32 s** |
| 生成速度 | 33.2 tok/s | **36.0 tok/s** |
| 一次问答端到端 | 2.2 s | **0.99 s** |
| 内存占用 | **21.5 GB** | 0 |
| 成本 | 0 | ¥0.0002 / 次 |
| 隐私 | **全本地** | 出网 |

结论：**DeepSeek 更快更省内存，本地模型的价值是离线 + 隐私 + 可控**。
调试时切着用——写 prompt 用云端（快、便宜），验证隐私场景用本地。

## 云端 API 配置

key 从三处读，优先级从高到低：

1. 本项目 `.env`
2. 环境变量
3. `~/Projects/algo-rag/.env` ← **复用**，本机只有这一份 DeepSeek key

界面只显示「有没有 key」和「从哪来的」，**永远不回传 key 本身**。

要给 OpenAI / Moonshot / 通义配 key，在 `~/Projects/model-studio/.env` 里写：

```bash
OPENAI_API_KEY=sk-...
MOONSHOT_API_KEY=sk-...
DASHSCOPE_API_KEY=sk-...
```

加自定义 provider：`config/providers.json`

```json
[
  {"id": "api__my_local", "name": "自建 vLLM", "base_url": "http://192.168.1.10:8000/v1",
   "model": "Qwen2.5-72B", "key_env": "", "note": "局域网"}
]
```

## 实测（本地模型）

| 项 | 值 |
|---|---|
| 启动到就绪 | **16 秒**（11 秒加载权重 + 5 秒起 HTTP） |
| 系统内存占用 | **21.5 / 24 GB**（模型跑起来后剩 10.6%） |
| 首 token | **0.52 秒**（热） |
| 生成速度 | **33.2 tok/s** |
| 流式 | 54 帧 / 2.18 秒 / 24.7 块每秒 |

## 代码结构与接口

### 后端 `app/`

```text
models.py      本地模型发现（扫 ~/Projects/*/models，纯文件特征，不猜）
providers.py   云端 API 预设 + key 解析（三处来源，不回传前端）
runner.py      模型进程生命周期（单例、接管遗留进程、启动参数）
gateway.py     OpenAI 兼容网关（/v1/*，路由 + 自动拉起 + 鉴权）
main.py        HTTP 接口 + SSE 转发 + 工具循环
agent.py       工具执行、上下文压缩、卡住检测、拒绝判定
persona.py     system prompt（有工具 / 无工具两套）
tools.py       20 个工具实现（沙箱、写保护、备份）
memclean.py    一键清理内存（停模型 + 杀残留 mlx 进程）
design.py      Playwright 布局检测 / 截图
memory.py      对话历史 + 长期记忆（jsonl + md）
gate.py        界面访问密码（sha256）
lock.py        模型控制锁（启停要密码）
tokens.py      多 token 管理（主 token + 附加 token）
```

### 前端 `web/src/`

```text
App.tsx                布局、状态轮询、门禁守卫
api.ts                 fetch / SSE 封装
components/
  ModelList.tsx        模型列表 + 启停 + 清理内存
  ChatPanel.tsx        流式对话 + 工具步骤 + 自动选型
  RagPanel.tsx         知识库在线提问
  ReplayPane.tsx       129 题回放（RAGAS 分数）
  MemoryPanel.tsx      长期记忆 + 历史
  DesignPanel.tsx      预览 + 截图 + 布局检测
  SettingsDrawer.tsx   生成参数 + 访问控制 + 主题 + 写保护
  AccessSection.tsx    门禁 / 模型锁 / Token
  GateLogin.tsx        门禁登录层
  StatusPanel.tsx      内存 / RSS / 端口
  LogView.tsx          模型日志
```

### 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/models` | 扫描本地模型 |
| GET | `/api/status` | 运行状态 + 内存 + rag 状态 |
| POST | `/api/start` | `{model_id, port, max_tokens, thinking}` |
| POST | `/api/stop` | 停止 |
| **POST** | **`/api/mem/clean`** | **一键清理内存（停模型 + 杀残留）** |
| POST | `/api/chat` | 非流式；`provider` 空=本地，`api__xxx`=云端 |
| POST | `/api/chat/stream` | SSE 流式，同一套 `provider` 语义 |
| **POST** | **`/api/agent/stream`** | **agent 模式：20 工具 + 多轮循环** |
| POST | `/api/rag/query` | 知识库检索（转发 :8601） |
| GET | `/api/rag/replay/list` | 129 题回放索引 |
| POST | `/api/design/shot` | 无头浏览器截图 |
| POST | `/api/design/check` | 布局问题检测 |
| GET/POST | `/api/tools/write-protect` | 写保护开关 |
| GET/POST | `/api/gate/*` | 界面门禁（status / password / verify / clear） |
| GET/POST | `/api/lock/*` | 模型锁（status / password / toggle / verify） |
| POST | `/api/tokens/*` | 多 token（list / add / toggle / remove） |
| GET | `/v1/models` | OpenAI 格式的可用模型列表 |
| POST | `/v1/chat/completions` | **Agent 入口**，流式/非流式都支持 |

## 四个关键设计决策

**1. 用 `start_new_session=True`，不用 `nohup`。**
`nohup` 只忽略 SIGHUP，挡不住终端退出时发给整个进程组的信号——在 algo-rag 上实测过：
脚本一结束服务就消失。`start_new_session` 让子进程脱离会话。

**2. 子进程 stdout 重定向到文件，不用 PIPE。**
没人读的 PIPE 写满 64KB 后子进程会阻塞，表现为"服务起了但不响应"。

**3. 就绪判定靠 HTTP 探活，不靠固定等待。**
`mlx_lm.server` 打完 "Starting httpd" 还要几秒才真能受理请求。现在轮询
`/v1/models` 直到 200，状态才置 `ready`。

**4. 默认关闭 thinking。**
Qwen3 默认输出 `<think>`。实测同一问题：开 thinking 时 80 token 全是思考、
11.5 秒给不出答案；关掉后 1.2 秒答完。所以启动命令固定带
`--chat-template-args '{"enable_thinking": false}'`。

## 已知限制

**进程 RSS 不可信。** MLX 用 mmap 加载 safetensors，这部分不计入 RSS，
所以界面上"模型进程 RSS"只有 3 GB，而实际系统占用是 21.5 GB。
**看内存请以"系统内存"那一行（带进度条）为准**，RSS 只作参考。

**同一时刻只能跑一个模型。** 24 GB 装不下第二个。要并存得换 64 GB 以上的机器。

**`mlx_lm.server` 不是生产级服务。** 官方自己警告「只实现了基本安全检查」。
本地自用没问题，别暴露到公网。

**GGUF 依赖外部 llama-server。** 通过 `~/.local/bin/llama-server` 找二进制；
找不到时该模型会标"未找到 llama-server"并且不能启动。本机用的是
`~/Projects/qwen38-local/llama-b11963/`（llama.cpp 0.8.0-dev）。

**thinking 开关需要重启模型。** 它写在启动命令的 `--chat-template-args` 里，
运行中改不了。界面上勾选后要停再启。

## 打包成 .app（可选）

方案 A（推荐，最省事）：`~/Desktop/ModelStudio.command` 双击 → 起服务 + 开浏览器。
已验证可用，等价于 `run.sh` + `open`。

方案 B（真 .app）：用 pywebview 包一层窗口：

```bash
.venv/bin/pip install pywebview   # 需要 pyobjc
python -c "import webview; webview.create_window('Model Studio', 'http://127.0.0.1:8600'); webview.start()"
```

再用 py2app 打包。**没做，因为打包容易在 pyobjc 依赖上翻车**，
而且 web 版已经够用。

## 还能加的

- **本地 vs 云端并排对比**（同 prompt 双发，左右分栏）—— 最值得做的下一步
- 模型下载（ModelScope 搜索 + 后台下载，复用 qwen-local 的 `dl_model.py`）
- prompt 模板库（保存常用 system prompt）
- 请求历史落盘（现在刷新页面就没了）
- token 计数按字符流估算（现在用块数，不准）

## 排障记录（2026-11-02）

### 排障：长提示连环空回复 / 自动重启循环

**现象**：agent 第一次长提示能答，之后连续空回复、界面反复「压缩上下文」「重启模型」；
短提示（「你好」）却一直正常。

**根因（日志实证，不是猜的）**，见 `logs/Qwen3-30B-A3B-4bit.log`：

```text
12:44:36 - Prompt Cache: 2 sequences, 1.02 GB
12:44:37 - ERROR - mlx_lm.server generation thread died:
    [METAL] Command buffer execution failed: Insufficient Memory
    (kIOGPUCommandBufferCallbackErrorOutOfMemory)
    ... mx.eval([c.state for c in self.prompt_cache])
127.0.0.1 - - "POST /v1/chat/completions HTTP/1.1" 404 -
```

30B 权重 16 GB + 24 GB 统一内存，长 system prompt（persona + 20 工具，约 5k token）
的 KV cache 每份约 0.5 GB；prefill 默认一次算 2048 token，Metal 峰值直接撑爆。
generation thread 死后 **HTTP 服务还活着**（`/v1/models` 200、TCP 通），但每个请求都
返回 404 + `{"error": "generation thread died"}`，且服务不自愈 —— 这就是「连环空回复」。

之前把锅甩给 `--kv-bits 8` 算坏缓存是错的，已在 `app/runner.py` 注释里更正。

**最终启动参数**（`app/runner.py::build_cmd`，都可用环境变量覆盖）：

| 参数 | 默认 | 环境变量 | 作用 |
|---|---|---|---|
| `--prompt-cache-size` | `1` | `MS_PROMPT_CACHE` | 只留 1 份 KV cache，杜绝累积到 2 份(1.02 GB) OOM；`0` 最稳但每轮重算 22 秒 |
| `--prefill-step-size` | `512` | `MS_PREFILL_STEP` | 把 2048 token 的大块 Metal buffer 切小，用时间换峰值内存 |
| `--kv-bits` | `8` | `MS_KV_BITS=0` 关闭 | KV cache 8bit 量化，内存减半，禁用 batching（agent 单请求无影响） |

**应用层兜底**：`app/main.py` 在 `/api/agent/stream` 与 `/api/chat/stream` 的 404 分支
按错误文本识别 `generation thread died` / `OutOfMemory` / `Insufficient Memory`，
自动重启本地模型一次；agent 还会重发本轮（不吃轮次）。空回复分支的「探活→压缩上下文」
救不了这种崩溃（TCP 通会被误判成进程活着），所以必须按错误文本精确识别。

**顺手修的坑**：自动重启原本把「本次请求的 `max_tokens`」当成服务端
`--max-tokens`（实测被压到 128，之后所有长回答都被截断）。现在 `Runner`
记录启动时的 `max_tokens` 并由 `status()` 回传，`main.py` 三处自动重启 +
`gateway.ensure_local_ready` 统一沿用，不再漂移。

**验证结果**：

- `cache=0`：5 次长提示 5/5 成功，但每轮 22 秒（不累积，最稳）
- `cache=1` 无 prefill 限制：首次 30.6 秒、后续 1.1 秒；10 轮动态 prompt 全通过，
  但 agent 带工具结果/历史时仍 OOM（唯一一份 cache 从 0.43 → 0.53 GB 不断变长）
- `cache=1 + prefill=512`：3 轮端到端 agent（天气 → 追问 → 再查天气，历史逐轮累积）
  全部通过，无 OOM / 空流 / 404；代价是单轮 22~51 秒
- **`kv-bits=8` 追加**：用户长请求（system+20 工具 schema+历史）实测 3984 token 在
  3072 token 处 OOM；开 `kv-bits 8` 后 4276 token 全程无 OOM。故默认改为 8。
- 自愈：用假服务模拟 `generation thread died`，agent 自动重启模型并重试成功

### 普通聊天也能查实时数据（只读轻工具）

**现象**：普通聊天问「中山天气怎么样」，自动选型选了本地 30B，模型背台词
「我无法直接获取实时天气数据」。原因：`get_weather` 只在 agent 模式可用，
`/api/chat/stream` 的 system 是 `with_tools=False`，模型手无寸铁。

**修法**：普通聊天开放 5 个**只读、无副作用**的联网工具，最多 2 轮工具循环：

`get_weather` / `get_exchange_rate` / `web_search` / `fetch_url` / `knowledge_search`

- 文件、命令、写操作仍然只属于 agent——聊天界面不该有能力删文件、跑命令。
- 只对**本地模型**启用：DeepSeek Reasoner 不支持 tools，传了直接 400。
- persona 的 NO_TOOLS 版本写着「不能联网」，所以 system 里当面追加一段更正
  （`_CHAT_TOOLS_NOTE`），否则模型拿着 schema 还是背「无法获取」的台词。
- 工具在 `asyncio.to_thread` 里跑，不堵事件循环；结果截 4000 字符回灌。

**踩的坑**：流式的 `tool_calls` delta 通常 content/reasoning 全空，会被
`if not piece and not reason: continue` 直接跳过——累积代码必须放在
continue **之前**，否则工具调用永远收集不到（第一版就栽在这，表现为空回复）。

**实测**：问天气 → 调 `get_weather` 返回真实数据（14.6s）；纯知识「1+1」
6.3s 直接答、不乱调工具；带历史追问「那深圳呢？」正确换城市再查。

### 让 30B 更像 agent：拒绝打回 + 工具优先铁律

本地 30B 最大的毛病是「该调工具时不调，直接说『我无法获取 / 做不到』」。
persona 里写了「天气用 get_weather」，它照样背台词。两处兜底：

- `agent.looks_like_refusal(answer, question)`：回答含拒绝词（无法/做不到/
  没权限/请你自己…）**且**问题指向有工具能干的活（天气/价格/最新/查/读/写/
  代码…）时判为「该调工具却拒绝」。agent 主循环据此打回一次，注入「你有这些
  工具」再重试。「你能飞吗 → 我不能飞」这类正确拒绝不受影响（问题没命中意图词）。
- `persona.WITH_TOOLS` 顶部加「第一条铁律：有工具就用工具，别用嘴拒绝」，
  工具分类列在最前面，给出「只要看起来能用工具做，就必须先试」的判断标准。

实测：问「最新 iPhone 价格」会自主走
`web_search → fetch_url ×N → make_chart` 的完整多步流程，出图并标注来源。

### 接入训练好的工具路由器小模型（0.5B / 18 分类）

agent 主循环第一轮会先让训练好的 0.5B 工具路由器判一下「大概率要哪个工具」，
把工具名（+ 规则初抽参数）作为提示注入 system，帮 30B 少瞎试：

- `training/route_once.py`：一次性入口（加载 0.5B → 分类 → 输出 JSON → 退出）。
  主程序的 .venv 没有 mlx-lm，所以用 qwen-local 的 python 走**独立子进程**，
  不引入常驻进程（避免再踩孤儿 mlx 进程的坑）。
- `app/main.py::_route_suggest`：子进程调用，超时 30s，失败静默返回 None。
- 只在「本地模型 + 问题疑似要用工具」（`agent.looks_like_tool_intent`）时调，
  纯寒暄/知识问答跳过，省 12 秒加载。
- 提示只作参考，**参数以 30B 自己解析为准**——实测路由器把 `app/main.py` 错抽成
  `/main.py`，30B 没照抄，自己纠正后正确读到文件。

开关：`MS_ROUTER=0` 关闭；`MS_MLX_PY` 指定装有 mlx-lm 的 python 路径。
实测：天气 51s 一次选对 `get_weather`（接入前 62s）；「1+1」17s 直接跳过路由器。
