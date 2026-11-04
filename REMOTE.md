# 从另一台设备用本地模型

> 2026-10-29。Mac 上跑模型，Windows / 手机 / 平板调它。

## 两条路，先选

| | 局域网直连 | SSH 隧道 |
|---|---|---|
| 需要开端口 | 是 | 否 |
| 需要 token | **是** | 否（走 SSH 身份验证） |
| 加密 | 否（明文 HTTP） | 是 |
| 配置成本 | 低 | 中 |
| 适合 | 家里/办公室可信网络 | 宿舍、校园网、公共网络 |

**我的建议：同一个家/办公室用局域网直连，其他场景用 SSH 隧道。** 不要在没有 token 的情况下把 8600 暴露到任何网络。

## A. 局域网直连

### Mac 端（已配好）

```bash
cd ~/Projects/model-studio
bash run.sh 8600 0.0.0.0        # 绑所有网卡，强制要求 token
```

`run.sh` 的行为：

- `run.sh` 不带第二个参数 → 只绑 `127.0.0.1`（默认，最安全）
- `bash run.sh 8600 0.0.0.0` → 绑所有网卡，**没设 `MODEL_STUDIO_TOKEN` 会直接拒绝启动**

token 已在 `.env` 里：

```bash
grep MODEL_STUDIO_TOKEN ~/Projects/model-studio/.env
```

Mac 的局域网 IP：

```bash
ipconfig getifaddr en0        # 本机现在是 172.19.219.3
```

### 安全边界（重要）

绑 `0.0.0.0` 之后，远程能做什么、不能做什么：

| 路径 | 远程（局域网） | 本机 |
|---|---|---|
| `/v1/chat/completions` | ✅ **需要 Bearer token** | ✅ |
| `/v1/models` | ✅ 需要 token | ✅ |
| `/api/*`（启停模型、管理） | ❌ **403 拒绝** | ✅ |
| `/` 管理界面 | ❌ 403 | ✅ |

**为什么这么设计**：`/api/*` 完全没有鉴权。如果放开，局域网里任何人都能
`POST /api/chat` 直接白嫖你的模型，或者 `POST /api/start` 把你的 16 GB 内存占满。
所以远程只能用 OpenAI 兼容端点，管理界面锁死在本机。

### Windows 端

**Python（OpenAI SDK）**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://172.19.219.3:8600/v1",
    api_key="<.env 里的 MODEL_STUDIO_TOKEN>",
)
r = client.chat.completions.create(
    model="local",                      # 自动拉起默认本地模型
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

**curl（PowerShell）**

```powershell
$t = "<你的 token>"
curl.exe -s http://172.19.219.3:8600/v1/chat/completions `
  -H "Content-Type: application/json" -H "Authorization: Bearer $t" `
  -d '{\"model\":\"local\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'
```

**WorkBuddy 自定义模型**

| 字段 | 值 |
|---|---|
| 接口地址 | `http://172.19.219.3:8600/v1` |
| API Key | `.env` 里的 token |
| 模型名称 | `local`（或 `deepseek-chat` 走云端） |
| 上下文长度 | 8192 |

### 可用模型

| model | 去向 |
|---|---|
| `local` | 本机第一个可用模型（未启动会自动拉起，首次 20~45 秒） |
| `Qwen3-30B-A3B-4bit` | 指定那个 30B MoE（33 tok/s） |
| `DeepSeek-R1-Distill-Qwen-14B-4bit` | 14B 推理模型（10.7 tok/s，思考链长） |
| `deepseek-chat` | 云端 DeepSeek |

## B. SSH 隧道（更安全，推荐外网用）

不开任何端口，不设 token，靠 SSH 认证。

**Mac 端**：系统设置 → 通用 → 共享 → 打开「远程登录」。

**Windows 端**（PowerShell 自带 ssh）：

```powershell
ssh -N -L 8600:127.0.0.1:8600 ziqilin@172.19.219.3
```

保持这个窗口开着，然后本机就用 `http://127.0.0.1:8600/v1`。

**优点**：

- 8600 不用绑 0.0.0.0，`run.sh` 默认状态就行
- 不需要 token（请求看起来来自本机）
- 流量加密
- 走校园网也能穿透（只要能 SSH）

**缺点**：窗口得一直开着。要长期用可以配 `~/.ssh/config` + 自动重连。

## 跨网络怎么办（不在同一个 Wi-Fi）

| 方案 | 说明 |
|---|---|
| Tailscale / ZeroTier | 装完就像在同一局域网，IP 固定。**推荐** |
| Cloudflare Tunnel | 免费，能绑域名，但公网可访问必须加 token |
| 端口转发 | 需要公网 IP，家用宽带多数没有 |
| ngrok | 免费版有延迟，不适合本地大模型 |

**本地模型带宽需求很高**：每次请求的 token 都要走网络，建议只透传结果不要透传长 prompt。
跨网络基本只能做文本问答，别指望流式体验。

## 常见问题

**连接超时 / 拒绝**

1. 服务没绑 `0.0.0.0`：`lsof -nP -iTCP:8600 -sTCP:LISTEN` 看是不是 `127.0.0.1:8600`
2. 两台设备不在同一网段：`ping 172.19.219.3`
3. **校园网/公司网可能开了 AP 隔离**，同一 Wi-Fi 也互相不可见。用 `ping` 验证
4. Mac 防火墙（本机现在是关闭状态，不用管）

**401 报错**

token 不对或没带 header。检查：

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer 你的token" http://172.19.219.3:8600/v1/models
```

**403 报错**

访问了 `/api/*` 或管理界面——这些只允许本机。远程只能用 `/v1/*`。

**第一次请求等 40 秒**

本地模型没启动，网关在自动拉起。**客户端的 timeout 必须 ≥ 120 秒**。

**IP 会变**

路由器重新分配 DHCP 后 `172.19.219.3` 可能变。两个办法：
- 路由器里给 Mac 绑定固定 IP
- 用 Tailscale（IP 永久固定）

## 实测记录

```
从 172.19.219.3 访问（模拟另一台设备）：
  /v1/models + token      → 200
  /v1/models 无 token     → 401
  /api/models             → 403（仅限本机）
  /                       → 403

完整调用：
  非流式（触发自动拉起）  → 33.7 秒，返回正确内容
  流式                    → 7 帧 / 0.6 秒
```


## 跨网络：Tailscale（已配好）

Mac 上已安装并登录。

| 地址形式 | 值 | 说明 |
|---|---|---|
| **设备名（推荐）** | `http://mac-mini:8600` | 好记，Windows 装了 Tailscale 后自动解析 |
| 完整域名 | `http://mac-mini.tailb15238.ts.net:8600` | MagicDNS 的完整形式 |
| Tailscale IP | `http://100.83.131.120:8600` | 最稳，不依赖 DNS |

### Windows 端只需两步

1. 到 tailscale.com/download/windows 下载安装，**用同一个账号登录**（就是 Mac 上登录的那个）
2. 浏览器打开 `http://mac-mini:8600/static/client.html`

装完在 Windows 上验证：

```powershell
ping mac-mini          # 或 ping 100.83.131.120
curl.exe http://mac-mini:8600/api/health
```

### 这套地址的好处

**在家、在学校、在咖啡厅、用手机热点，地址完全不变。** 不用查 IP，不用改配置。

### 实测

```
http://100.83.131.120:8600/api/health          → 200
http://100.83.131.120:8600/static/client.html  → 200 (25910 字节)
http://mac-mini:8600/api/health                → 200（MagicDNS 生效）
/api/models 带 token                           → 200
/api/models 无 token                           → 401
/v1/models 带 token                            → 200
```

### 三个注意事项

**1. Mac 休眠会断连。** 笔记本合盖就睡，Tailscale 掉线，Windows 那边访问超时。
插电长期用的话关掉休眠：

```bash
sudo pmset -a sleep 0 disksleep 0
```

（需要开机密码。只想临时保持可以用 `caffeinate -s`，Ctrl+C 结束。）

**2. 打洞失败会走中转。** 检查方式：

```bash
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

设备后面显示 `direct` 是 P2P 直连（延迟低），`relay` 是走 DERP 中转（延迟 300~500ms，但文本问答仍可用）。

**3. token 现在是唯一防线。** 一旦能跨网络访问，端口被扫描的概率上升：

```bash
grep MODEL_STUDIO_TOKEN ~/Projects/model-studio/.env    # 必须是 24 字节随机串
```

**不要把 .env 提交进 Git，也不要改成简单密码。**

### 如果某天 Tailscale 连不上

国内网络下 Tailscale 的控制服务器（login.tailscale.com）偶尔访问困难。现象是 Windows 上 `tailscale status` 一直 `connecting`。

备选：

| 方案 | 说明 |
|---|---|
| ZeroTier | 同类工具，planet 服务器国内可达，配置稍复杂 |
| Cloudflare Tunnel | Mac 上 `cloudflared tunnel --url http://127.0.0.1:8600`，给你一个临时 https 域名，Windows **不用装任何东西** |
| frp + 公网 VPS | 最稳，但要买服务器（约 ¥30/月） |

## 具体客户端配置

### WorkBuddy（Windows）

设置 → 模型 → 添加模型：

| 字段 | 值 |
|---|---|
| 模型类型 | 自定义 API |
| 接口地址 | `http://172.19.219.3:8600/v1` |
| API Key | `.env` 里的 `MODEL_STUDIO_TOKEN` |
| 模型名称 | `local` |
| 上下文长度 | 8192 |
| 最大输出 | 2048 |

高级配置只勾两项：**工具调用**、**自定义协议**。其余（图片输入、思考模式、思考强度）全不勾——
本地模型不支持。

### DeepSeek Harness（dsh）

> 开源 agent runtime，MIT，Node.js。`npx @deepseek-ai/dsh web` → 浏览器 `127.0.0.1:3080`。
> 一切皆插件，模型也是插件，所以能换成任意 OpenAI 兼容端点。

**Settings → Models → Add model provider → Custom model API**

| 字段 | 值 |
|---|---|
| Provider ID | `mac-local`（小写唯一） |
| Base URL | `http://172.19.219.3:8600/v1` |
| Protocol | `openai-completions` |
| Credentials | `.env` 里的 token |
| Model ID | `local` |

先点 **Fetch available models**，能列出 `local` / `Qwen3-30B-A3B-4bit` / `deepseek-chat` 就说明通了。
列不出来就手动填 `local`。

**实测兼容性**（这几项 dsh 都可能发，我在网关上逐个验过）：

| 请求特征 | 结果 |
|---|---|
| 标准 `user` role | ✅ 200 |
| `developer` role | ✅ 200（能处理，语义当普通消息） |
| `max_completion_tokens` 字段 | ✅ 200（能接受） |
| `tools` 函数定义 | ✅ 200，返回正确的 `tool_calls` |

所以默认不用改兼容配置。如果发现 system prompt 不生效或输出被截断，在 provider 配置里加：

```yaml
compat:
  supportsDeveloperRole: false    # 让 dsh 把 developer 转成 system
  maxTokensField: max_tokens      # 用 max_tokens 而不是 max_completion_tokens
```

配置文件位置：`$DSH_HOME/profiles/web/cordis.patch.yml`，凭证在 `$DSH_HOME/.credentials.yaml`。

### 但是：本地模型跑 dsh 会很痛苦

dsh 是 agent，一次任务要：读文件 → 思考 → 调工具 → 看结果 → 再调……每轮都是一次完整请求。

算一笔账（本地 30B，33 tok/s，8k 上下文）：

| 环节 | 成本 |
|---|---|
| 每轮工具调用的 prompt | 2000~4000 token（含历史） |
| 每轮 prefill | 2000 token ÷ 20 tok/s ≈ **100 秒** |
| 每轮生成 | 200 token ÷ 33 tok/s ≈ 6 秒 |
| 一个 5 轮的小任务 | **约 9 分钟** |

**结论**：dsh + 本地模型只适合"问一句答一句"，不适合让它自己改代码、跑测试。

**更实际的做法**：dsh 里配两个 provider——

- `mac-local` → `model: local`（本地，白嫖、离线、短问答）
- `deepseek-cloud` → `model: deepseek-chat`（走同一个网关，但上游是 DeepSeek API，快且便宜）

会话里按任务切换。同一套工具、同一套 session 记录，只换模型。

### 三者的定位

| 客户端 | 适合本地模型的场景 |
|---|---|
| OpenAI SDK / curl | 脚本、批处理、测速，最适合 |
| WorkBuddy | 单轮问答、轻量任务（摘要/翻译/润色） |
| dsh | 不建议跑完整 agent 任务，只做单轮对话 |

## Windows 客户端（已做）

不用装任何东西。Windows 浏览器打开：

```
http://172.19.219.3:8600/static/client.html
```

第一次会让你填三样：Mac 地址、端口、token。填完存本地（localStorage），下次直接进。

**功能和你 Mac 上看到的一样**：模型列表、启停、流式对话、思考过程折叠、内存监控、日志、参数滑杆。

### 为什么不是独立 exe

| 方案 | 优点 | 缺点 |
|---|---|---|
| **浏览器打开（现在的）** | 零安装、跨平台、改了就生效 | 需要一个浏览器标签页 |
| 打包成 exe | 双击就开、有独立图标 | 要装 Python + PyInstaller 打包，还要处理 WebView2 依赖，体积 100 MB+ |

**要做 exe 的话**（在 Windows 上跑）：

```powershell
pip install pywebview pyinstaller
# 写一个 5 行的启动脚本，用 webview.create_window 加载那个 URL
pyinstaller --onefile --windowed launcher.py
```

但说实话没必要——浏览器打开一个标签页，和独立窗口的差别只在心理上。**功能完全一样。**

### 离线版（不依赖 Mac 的网页服务）

`windows-client.html` 这个文件本身可以直接拷到 Windows，双击用浏览器打开（`file://` 协议）。
两种方式都行：

- **同源方式**（推荐）：访问 `http://172.19.219.3:8600/static/client.html`，走 Mac 的静态托管
- **本地文件方式**：拷 `windows-client.html` 过去双击，跨域请求靠 CORS 放行

### 当初为什么要改权限模型

第一版是「远程一律 403，只能用 `/v1`」——安全，但**连模型都启不了**，客户端等于残废。

现在是「本机免 token，远程带 token 全放行」：

| 路径 | 本机 | 远程（无 token） | 远程（有 token） |
|---|---|---|---|
| `/`、`/static/*` | ✅ | ✅ | ✅ |
| `/api/health` | ✅ | ✅ | ✅ |
| `/v1/*` | ✅ | ❌ 401（gateway 校验） | ✅ |
| `/api/models`、`/api/status` | ✅ | ❌ 401 | ✅ |
| `/api/start`、`/api/stop` | ✅ | ❌ 401 | ✅ |
| `/api/chat/*` | ✅ | ❌ 401 | ✅ |

安全性由 token 保证（24 字节 URL-safe 随机串），不靠"锁 IP"。

### 踩过的两个坑

**1. CORS 预检被自己的 middleware 拦了。** 浏览器发 OPTIONS 时不带 `Authorization`，
middleware 判成未授权返回 401，浏览器就不会发真实请求。**必须先放行 OPTIONS**，
交给 CORS 中间件回 200 + `access-control-allow-origin`。

**2. `middleware` 里用 `os.environ` 但文件没 `import os`。** 表现是远程请求全 500、本机正常
（因为本机走的是免 token 分支，没执行到那行）。这种 bug 只有真的从另一台机器访问才会暴露。

