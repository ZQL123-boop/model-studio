# Model Studio 问题复盘（2026-11-08 22:43 ~ 11-09 21:33）

11 个 commit，11 类问题。全部改动已推送到 `github.com/ZQL123-boop/model-studio`。
回归测试从 0 增加到 **85 个用例**（4 个脚本，全绿）。

---

## 一、总览

| # | 问题 | 类型 | 一句话根因 | commit |
|---|---|---|---|---|
| 1 | 「生成一张猫咪的照片」只道歉不出图 | 功能 bug | guard 用连续子串白名单判图像意图，「生成**一张**猫咪的**照片**」没命中 | `8664321` |
| 2 | 改完代码「还是不行」 | 运维 | 服务用 `uvicorn` 不带 `--reload`，进程里还是旧代码 | 无（重启） |
| 3 | 图出来了但界面显示 `![...](...)` 原文 | 前端 | 气泡把内容当纯文本渲染，没解析图片语法 | `0f19948` |
| 4 | 「再生成一张」跑去联网找图，最后说服务不可用 | 工具层设计 | 生图临时 402/503 被当"工具永久失败"，模型按 persona 转去 `web_search` | `9776f27` |
| 5 | 评测报告 6 项缺陷（权限 45 分、效率 60 分） | 安全 + 策略 | 白名单只做 schema 层过滤；模型把"没有"补成"编一个写库" | `2cd83b8` |
| 6 | 「再生成一张**小狗**照片」出的是猫 | **我的回归** | 指代判定太宽，含「再…张」就复用旧 prompt，把新对象丢了 | `c02beac` |
| 7 | 缺「清空对话」入口 | 功能新增 | — | `c02beac` |
| 8 | 第二轮回答停在空白气泡，分不清死活 | 前端 | SSE 提前结束时不发 `done` 帧，前端两个分支都不绘制 | `70eb77b` |
| 9 | 追踪页面永远空白 | 前端架构 | 切 tab 时 ChatPanel 卸载并 `abort()` 正在跑的流 | `13a5a0a` |
| 10 | 自动选型按关键词把请求送去付费 API | 功能移除 | 需求变更：整体删掉 | `011ccce` |
| 11 | 没开 agent 却"创建了 `~/Projects/sort/quick_sort.c`" | 模型幻觉 | 普通聊天只有 5 个只读工具，模型编了路径，文件从不存在 | `c756a9c` |
| 12 | 公网用户能选云端模型（花主人的钱） | 安全 | 前端有下拉框就能选，后端不校验来源 | `43d6510` |
| 13 | `write_plan` 连调 6 次后 `IndexError` | 崩溃 | 游标允许等于 `len(plan)`，取文案时直接索引越界；且绕过了重复检测 | `888d97b` |

**按根因归类**

| 类别 | 数量 | 说明 |
|---|---|---|
| 代码缺陷 | 6 | #1 #3 #6 #9 #13 + #5 的白名单部分 |
| 模型能力/幻觉 | 2 | #4（把临时错误当永久失败）、#11（凭空声称写文件） |
| 安全边界缺失 | 2 | #5（白名单只在 schema 层）、#12（公网可选模型） |
| 前端交互 | 3 | #3 #8 #9 |
| 运维/环境 | 2 | #2（没重载）、git push DNS 坏 IP |
| 我的失误 | 2 | #6（引入回归）、#11 修复时 `UnboundLocalError` |

---

## 二、逐条详情

### 1. guard 误判图像意图，只道歉不出图 `8664321`

**现象**：26.8s 后只回一句「我之前错误地调用了生成图片的工具」，没有图。

**根因**：`guard.py` 用连续子串 `("画","生成图","来张","插画","海报","配图")` 判断要不要图。「生成**一张**猫咪的**照片**」既没有连续的"生成图"，词表里也没有"照片" → 判成"用户没要图" → 拦截结果当 tool result 回灌 → 30B 道歉并结束。生图服务本身正常（Pollinations 0.9s / 256px）。

**修法**：
- 图像意图改宽松正则 `_IMAGE_HINT`，容忍动作与名词之间插量词/修饰语。
- 拦截策略从「白名单没命中就拦」改成「**明确纯文本任务才拦，拿不准就放行**」。
  误杀代价（要图却只收到道歉）远大于漏放代价（多跑 1 秒生图）。
- 拦截语补明确指令（不要道歉、不要解释、直接文字回答），否则 30B 会摆烂。
- 新增 `scripts/test_guard.py`。

**验证**：20/20；主路径实测 8.1s 出图。

---

### 2. 改完代码「还是不行」——服务没重载（无 commit）

**现象**：22:47 又失败一次（38.9s，同一句道歉）。

**根因**：`lsof` 显示 8600 还是旧 PID；`guard.py` 22:42 才改完 → 进程里跑的是旧代码。会话存档 `ece1d288.json` 显示 `steps=[]`、`turns=3`，与旧词表的判断完全吻合。

**处理**：kill 后重启（PID 5715）。端到端验证 78.1s 出图，图片 URL 实测 `200 / image/jpeg / 54197B`。

**结论**：`run.sh` 用 `uvicorn app.main:app` **不带 `--reload`**，改 Python 必须重启进程。

---

### 3. 图片显示成原始 Markdown `0f19948`

**现象**：43.8s、工具标签正常，气泡里只有一行 `![小猫的照片](/generated/img_xxx.jpg)` 纯文本。

**根因**：后端没问题（URL 实测 200 / image/jpeg / 60KB），是 `ChatPanel.tsx` 把 `{m.content}` 当纯文本塞进气泡。

**修法**：
- 加 `MessageBody`，**只解析 `![alt](url)` 一种语法**渲染成 `<img>`，其余原样保留。不引 marked / react-markdown（新依赖 + XSS 面）。
- URL 白名单：同源 `/generated|shots|static|assets` + http(s)；`javascript:` / `data:` 不渲染。
- 带 `g` 的正则每次重置 `lastIndex`，否则多条消息会串位。

**验证**：build 后强刷，界面正常显示图片。

---

### 4. 「再生成一张」跑偏到联网找图 `9776f27`

**现象**：工具链 `generate_image ×2 + web_search ×2 + fetch_url ×3`，最后「图像生成服务暂时不可用」。**9 轮 / 158.9s / 0 张图**。

**根因**（存档 `002f5502.json`）：
1. `generate_image(prompt="再生成一张")` → HTTP **503**
2. `generate_image(prompt="一只可爱的小猫")` → HTTP **402**
3. 工具把 `[错误] HTTP 402` 原样回灌 → `persona.py:58`「只有工具真失败才允许说做不到」→ 模型判定"做不到" → 按「找信息→联网」跑去搜图 → 编结论。

而直接调生图 **1.9s 成功**——不是服务挂了，是 Pollinations 间歇性 402/503 抖动。

**修法（全在工具层，不靠模型自觉）**：
- `generate_image` 内部重试 402/408/425/429/5xx + 网络异常：4 次尝试，退避 3/6/12s；每次换 seed，第 3 次起切备用入口 `pollinations.ai/p/`。400 等非临时错误不重试。
- 重试耗尽返回 `[临时故障]` + **明确指令**：禁止切 `web_search`/`fetch_url`、禁止编造、直接再调一次。这条是关键——只说「服务不可用」会被复述成最终答案。
- 「再生成一张」复用 `_LAST_IMG_PROMPT`；随机 seed 保证是新图；文件名改毫秒级防覆盖。

**验证**（真实两轮 HTTP，第 2 轮遇到**真 402**）：第一次调用返回临时故障 → 模型没跑偏、按指令重试 → **2 次调用 / 52.9s / 1 张图**。回答还如实说明"被限流，重试后成功"。

---

### 5. 按评测报告修 6 项 `2cd83b8`

报告：17 用例 / 19 次真实推理 / **37/40（B+）**，机械断言 + 人工复核。自评里权限与安全边界 **45 分**、效率 **60 分**，其余 85~92。

| 缺陷 | 修法 |
|---|---|
| **高危** 白名单只在 schema 层过滤（`main.py:1145`），派发处无校验 | `tools.call` 加 ContextVar 请求级白名单（`to_thread` 会 copy_context，覆盖并行只读分支）+ `guard_remote` 中间件统一设置 + 派发处显式拦截 |
| **高危** `list_goals` 说没有 → 模型造 4 条写库 → 汇报「您有 4 个」 | `guard_tool` 扩展 `last_result`：紧接"没有/为空"的写库调用拦截；新增 `grounded_in_user`（字级 2-gram 覆盖 ≥0.6 才算用户原话）；`done=True` 不校验 |
| 中 单次工具失败即放弃 | 失败计数从"连续 2 次"改成"**首次失败**就推换 `find_files`/`list_dir`" |
| 中 RAG 弱命中不换路（top1 相似度 0.1538） | `knowledge_search` 在 `score<0.35` 时追加换关键词/转 `web_search` 提示 |
| 中 伪推理链（答案对、过程编） | STYLE 加「推理链必须能逐句核对，缺依据只给结论并标注不确定」 |
| 低 只读 1 行就总结 | `read_file` 描述补「总结类至少读 80 行」 |

**踩到的坑**：`tools.py:2435` 的 `_compact_schemas` 会把工具描述截到**第一句 52 字**，追加的提示根本进不了模型上下文 → 必须把 `read_file` 加进 `_KEEP_FULL_DESC`。**改 schema 描述后一定要验运行时的 `tools.TOOLS`，不是看源码。**

**顺手清了真实脏数据**：`data/goals.json` 里 4 条假目标（22:31:18–23 连续生成），备份到 `~/Projects/model-studio-goals-20261108.json`。

**报告里有一处误判**：T10 说「访客越白名单执行了 `write_file`」——不成立。`REMOTE_TOOLS` 本来就含 `write_file`，且有 `REMOTE_OUT_DIR` 路径重定向。真正的漏洞是 `set_goal`/`run_bash`/`edit_file`/`spawn_agents`/`design` 这些**完全不在白名单**的工具没被拦。

---

### 6. 「再生成一张**小狗**照片」出猫 `c02beac` — 我的回归

**现象**：出图但是猫，58.3s。

**根因**：上一轮我加的 `_is_repeat_prompt` 只要句子含「再…张」就判成纯指代、复用 `_LAST_IMG_PROMPT`，把「小狗」这个新对象丢了。

**修法**：换成 `resolve_image_prompt(prompt, last)`，剥掉元指令前缀后看还剩什么：

| 输入 | 结果 |
|---|---|
| `再生成一张` | 复用上一张 |
| `再生成一张小狗照片` | `小狗照片`（不复用猫）|
| `再生成一张，水彩风` | `上一张描述，水彩风`（追加风格）|
| `一只橘猫坐在窗台上` | 原样用 |

**验证**：`test_generate_image` 补 7 例 → 26/26；真实两轮：猫 9.5s → 要狗时工具 prompt 是「一只可爱的小狗，金毛幼犬…」，无猫残留。

**教训**：给「指代复用」加宽松匹配，必须测「指代 + 新对象」这种混合输入。

---

### 7. 清空对话 `c02beac`

- `col-head` 加按钮，**二次确认**（4 秒自动收回），正在回答时禁止并提示。
- 清 `msgs` / `sessionStorage["ms_chat_msgs"]` / 输入框。
- 新增 `POST /api/agent/clear` → `tools.reset_image_memory()`。**不能漏**：消息历史在前端、每次请求自带，服务端唯一跨请求状态就是 `_LAST_IMG_PROMPT`，不清的话新对话说「再生成一张」还会冒出清空前的猫。

---

### 8. SSE 提前结束留空白气泡 `70eb77b`

**现象**：第一轮 71.7s 出图；第二轮停在空白灰色气泡，12 分钟没有会话存档。

**根因**：存档只在请求正常走到 `done` 时写 → 那次流没正常结束。`api.ts` 的 `streamSSE` 在服务端/网络提前关流时 `reader.read()` 返回 `done=true`，generator 直接结束，前端收不到 `done` 帧；ChatPanel 只在「模型没输出」和「用户取消且已有文字」时绘制，一个字都没写出来就永久空白。

**修法（纯前端）**：加 `gotDone` 标志——流结束但没收到 `done` 帧 → 气泡写「连接提前结束…直接重发一次即可」；用户主动停止且无输出 → 显示「（已停止）」。

**没有缩短生图重试退避**：71.7s 那次存档里只有 1 个成功 step、没有 `[临时故障]`，说明工具一次成功、没触发 21s 退避。瓶颈是 30B 多轮推理 + 在线生图本身，缩短退避既无效又降低成功率。

---

### 9. 追踪页永远空白 `13a5a0a`

**根因（两个叠加）**：
1. **主因**：`App.tsx` 用 `{tab === "chat" && <ChatPanel/>}` 条件渲染。切到「追踪」tab 时 ChatPanel 卸载，它的 cleanup 会 `abort()` 正在跑的 SSE 流 → `step` 帧发不出来 → 追踪页空白、对话也断在半截。**和 #8 是同一个根因**，上次只修了症状。
2. `traces` 只是 `useState([])`，强刷就全空。

**修法**：
- ChatPanel 常驻，包一层 `.chat-pane`，切 tab 只加 `.hidden`（`display:none`），不卸载。`abort` 只在整个 App 卸载时触发。
- `traces` 落 `sessionStorage["ms_traces"]`，保留最近 200 条。

**验证**：build 产物正常；源码里 `tab === "chat" &&` 残留 0 处。

**注意**：修复前的工具调用没有持久化，不会自动回填。

---

### 10. 移除自动选型 `011ccce`

需求变更：先要求"公网不可用"，随后改为"**全部去掉**"。

- 删 `app/router.py`（5 条关键词规则）。
- `main.py` 去掉 `import router` / `_auto_pick()` / `Target.reason` / 两处 SSE `route` 帧；老客户端传 `provider="auto"` 显式归一化为空 → 走本地，不报错。
- 前端删「自动选型」option、`route` 类型、`routechip` 渲染与 CSS；默认 provider 改 `""`。
- 旧回退页 `static/app.html` 同步清理（否则 dist 丢失回退时又冒出来）。
- README 3 处描述、训练脚本里的示例路径一并改。

**验证**：产物无「自动选型」/`routechip`/`value="auto"`；真实 HTTP 两路（传 auto / 不传）均走本地、均无 route 帧。

---

### 11. 普通聊天幻觉「已创建 xxx.c」`c756a9c`

**现象**：没开 agent，问「用c写一个快速排序」，回答「已创建快速排序程序，文件路径为 `~/Projects/sort/quick_sort.c`」。

**查证**：`~/Projects/sort/` **目录都不存在**。普通聊天的 `CHAT_TOOL_NAMES` 只有 5 个只读工具，没有 `write_file` → 纯幻觉。

**修法**：
- `_CHAT_TOOLS_NOTE` 加第 4 条：明令禁止「已创建/已保存/文件路径为」，要求代码贴回答里 + 「保存为 xxx.c」。
- `_FAKE_FILE_CLAIM` 正则（跨行、必须带扩展名或绝对路径，正常代码回答不误伤）+ `_rewrite_no_file_claim()`：命中就非流式重写一次；失败则前置一句"文件并不存在"。
- `done` 帧新增 `answer` 字段（前端会用它覆盖增量文本），修正才能显示。

**修复过程中我自己引入的坑**：在 `gen()` 里写 `_acc = [...]` 赋值，Python 把 `_acc` 变成 gen 的**局部变量** → 前面所有 `_acc` 读取全部 `UnboundLocalError` → `/api/chat/stream` 返回 **200 + 0 字节**，连 1+1 都空。改用独立变量 `_final`。

**排查教训**：`tail err.log` 看不到 Starlette 吞掉的 traceback，最后靠 `TaskOutput` 抓后台 shell 的 stdout 才定位。**流式接口返回 200+0B，优先怀疑生成器里未捕获异常。**

---

### 12. 模型选择只对本机开放 `43d6510`

**原则**：前端藏控件不算数——公网直接 POST `provider:"api__deepseek"`、或在 `/v1` 点名 `model:"deepseek-chat"` 都能绕过。

**后端（真正的拦截）**：

| 入口 | 处理 |
|---|---|
| `/api/chat`、`/api/chat/stream`、`/api/agent/stream` | `resolve_target(..., visitor=True)` 时 `api__*` 一律降级本地 + 打日志 |
| `/v1/chat/completions` | `gateway.resolve(..., visitor=True)` 同样落本地；降级后照常 `ensure_local_ready`，不甩 503 |

**前端**：ChatPanel 非本机不渲染 `select`，换虚线锁定标签「本地模型（公网不可切换）」；ComparePanel 非本机整页提示（early return 必须放在所有 hooks 之后）。

**验证**：访客 `/api/chat` 点名 `api__deepseek` → `provider=Qwen3-30B`、`remote=false`；访客 `/v1` 点名 `deepseek-chat` → 返回 `model=default_model`。

---

### 13. `write_plan` IndexError `888d97b`

**现象**：连调 6 次 `write_plan` 后抛 `IndexError: list index out of range`，124s 白跑，用户什么都没拿到。

**根因（两处，都在 `agent.py` 的 write_plan 分支）**：
1. `new_cursor = min(current, len(plan))` 允许等于 `len(plan)`（表示全部完成），但随后直接 `state.plan[state.cursor]` 取文案 → 到末尾必越界。重复分支里的同一句也会越界。
2. `write_plan` 在 `state.push()` **之前** return，完全绕过重复检测 → 那套「连续 3 次相同操作算卡住」对它无效，所以能连调 6 次把轮次耗光。

**修法**：
- 新增 `_plan_step_text()`：游标不在 `[0, len)` 时返回「（计划已全部完成）」，两处都走它。
- 加 `plan_repeats` 计数：游标到末尾还重复调 → 直接收口；原地踏步第 3 次 → 收口；前两次仍只提示。推进时归零。
- 修掉"共 4 步却显示第 5 步"。

**验证**：新增 `scripts/test_plan.py` 13 例；真实复现：IndexError 消失，5 次 write_plan 后正常给出完整 C 代码。

---

## 三、遗留未修

| 项 | 为什么没做 |
|---|---|
| **`write_plan` 每步重发全量 steps**（评测缺陷 5） | 要改模型侧协议（steps → 只回 current，服务端合并），风险大于省下的 4 轮 |
| **路由小模型判 `no_tool`**（缺陷 7） | 要动工具路由主链路 |
| **空回复重试导致固定 3 轮**（缺陷 9） | 碰生成主循环，容易引入新回归 |
| **`MAX_TURNS` 放宽** | 报告自己说先治轮次浪费，否则只是更慢 |
| **未重跑 17 条完整评测** | 所以本文件**不报新分数**，只报修复项的行为验证 |

---

## 四、环境坑（对以后的自己）

1. **服务不带 `--reload`**：改 Python 必须重启。`run.sh` 用 `uvicorn app.main:app`。
2. **`web/dist` 在 `.gitignore`**：改前端源码必须 `cd web && npm run build`，且用户要强刷（hash 变了）。
3. **sandbox 只放行 `curl` 的网络**：`git`/`node`/`ollama` 的网络进程会被 SIGTERM，需 `dangerouslyDisableSandbox`。
4. **本地服务要 `--noproxy '*'`**：否则被 `HTTP_PROXY` 拦成 502。
5. **GitHub DNS 会返回坏 IP**：`20.205.243.166` 不通但 `140.82.113.3` 等可用。解法（不改 hosts、不改全局 config）：
   ```bash
   env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy \
     GIT_TERMINAL_PROMPT=0 \
     git -c http.version=HTTP/1.1 \
         -c http.curloptResolve="github.com:443:140.82.113.3" \
         push origin main
   ```
   `--resolve` 保留 SNI，证书正常。**不要写进 git config**——IP 会变，硬编码哪天反而让 push 失败。
6. **改 schema 描述要看运行时**：`_compact_schemas` 会截到第一句 52 字。
7. **闭包里别赋值同名外层变量**：会变成局部变量，前面读取全炸。

---

## 五、验证资产

```bash
cd ~/Projects/model-studio
for f in scripts/test_*.py; do .venv/bin/python "$f"; done
```

| 脚本 | 用例 | 覆盖 |
|---|---|---|
| `test_guard.py` | 23 | 图像意图判定、`set_goal` 编造写入、`grounded_in_user` |
| `test_generate_image.py` | 26 | 限流重试/退避/备用入口、指代解析、混合输入 |
| `test_eval_fixes.py` | 23 | 访客白名单（含 `to_thread`）、set_goal 护栏、首次失败换路、RAG 弱命中 |
| `test_plan.py` | 13 | `write_plan` 越界、原地踏步收口、空 steps |

**合计 85 个用例，全部离线**（mock 网络，不烧 API 额度、不写真库）。
