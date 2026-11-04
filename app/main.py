"""Model Studio 后端：本地模型开关 + 云端 API 调试。

两件事共用一套接口，靠 `provider` 字段区分：
  - 不传或传本地模型 id → 走本机 mlx_lm.server 进程
  - 传 `api__xxx`        → 直接转发到云端，不需要启动任何进程

只做本地用途，绑 127.0.0.1。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import agent, gate, gateway, guard, lock, memclean, memory, models, persona, providers, rag, router, scheduler, sysinfo, tokens, tools
from .gateway import ensure_local_ready
from .runner import LOG_DIR, runner

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"

import threading as _th
import time as _time


def _schedule_loop():
    while True:
        try:
            scheduler.check_due()
        except Exception:                                 # noqa: BLE001
            pass
        _time.sleep(30)


_th.Thread(target=_schedule_loop, daemon=True).start()


app = FastAPI(title="Model Studio", version="0.3.0")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

# 独立客户端（Windows 上的 HTML/exe）可能是 file:// 来源，origin 是 "null"，
# 所以不能枚举白名单。安全性由 token 保证，不靠 CORS。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


@app.middleware("http")
async def guard_remote(request, call_next):
    """访问控制：本机全放行，远程要 Bearer token。

    演进过程：
      最初是"远程一律 403，只能用 /v1" —— 安全但没法做远程客户端。
      现在改成"远程带 token 就能用全部接口"，因为 token 是 24 字节随机串，
      强度足够；而完全锁死会让 Windows 客户端连模型都启不了。

    放行的：静态资源、首页（只是一张 HTML，没有敏感信息）。
    """
    # CORS 预检不带业务 header，必须放行，否则浏览器根本不会发真实请求
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    # 这些接口放行（返回的只是布尔状态，不敏感）：
    # 登录层/锁按钮/公网按钮要在填 token 之前就能查到状态。
    # 不放行的话，公网端填 token 前 status 请求 401，前端状态停在默认值，
    # 导致"锁图标提示设密码""启动不弹密码框"。
    if path == "/" or path.startswith("/static") or path == "/api/health" \
            or path.startswith("/api/gate") \
            or path == "/api/lock/status" \
            or path == "/api/public/status":
        return await call_next(request)
    if path.startswith("/v1"):
        return await call_next(request)          # gateway 内部自己校验

    host = request.client.host if request.client else ""
    # funnel/serve 会把公网请求代理到 127.0.0.1，单看 host 会误判成本机，
    # 导致公网访问也免 token。转发来的请求带 X-Forwarded-For，据此区分：
    #   - 真本机（浏览器直连 127.0.0.1，无 XFF）→ 免 token
    #   - funnel 转发（host=127.0.0.1 但带 XFF）→ 照常要求 token
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if host in LOCAL_HOSTS and not forwarded:
        return await call_next(request)          # 真本机免 token

    token = os.environ.get("MODEL_STUDIO_TOKEN", "").strip()
    if not token:
        return JSONResponse(
            {"detail": "服务绑定了非回环地址但没设 MODEL_STUDIO_TOKEN，拒绝远程访问"},
            status_code=403)
    got = request.headers.get("authorization", "")
    if not (got.lower().startswith("bearer ") and tokens.verify(got[7:].strip())):
        return JSONResponse(
            {"detail": "需要 Authorization: Bearer <token>",
             "hint": "token 在 Mac 的 .env 里：# grep MODEL_STUDIO_TOKEN ~/Projects/model-studio/.env"},
            status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------ 模型 / API 列表
@app.get("/api/models")
def list_models():
    local = [m.to_dict() for m in models.discover()]
    remote = [p.to_public() for p in providers.load_providers()]
    return {"local": local, "remote": remote,
            "count": len(local) + len(remote)}


# ---------------------------------------------------------------- 知识库自启
# 为什么放在 app 里而不是只管 run.sh：
# 这个服务由 launchd 托管（KeepAlive），也可能是手动 run.sh、IDE 里起的。
# 只在 run.sh 里拉知识库，一旦 app 被别的方式拉起，knowledge_search 就会
# 静默降级成联网搜索。所以由进程自己保证"我起来时知识库也得在"。
_RAG_PROC: "subprocess.Popen | None" = None
RAG_DIR = Path(os.environ.get("RAG_DIR", str(Path.home() / "Projects" / "algo-rag")))
RAG_URL = os.environ.get("RAG_URL", "http://127.0.0.1:8601")


def _rag_alive() -> bool:
    try:
        with httpx.Client(timeout=1.5) as c:
            c.get(f"{RAG_URL}/health")
        return True
    except Exception:                                     # noqa: BLE001
        return False


def _ensure_rag() -> str:
    """知识库没起来就拉起来。已经在跑则什么都不做（幂等）。"""
    global _RAG_PROC
    if _rag_alive():
        return "already-running"
    py = RAG_DIR / ".venv" / "bin" / "python"
    if not (py.exists() and (RAG_DIR / "serve.py").exists()):
        return f"missing（{RAG_DIR}）"
    (RAG_DIR / "logs").mkdir(parents=True, exist_ok=True)
    log = open(RAG_DIR / "logs" / "serve.log", "a", encoding="utf-8")   # noqa: SIM115
    try:
        _RAG_PROC = subprocess.Popen(
            [str(py), "serve.py", "8601"], cwd=str(RAG_DIR),
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        return f"started pid={_RAG_PROC.pid}"
    except Exception as e:                                # noqa: BLE001
        return f"failed: {type(e).__name__}: {e}"


_RAG_WD_STOP = _th.Event()


def _rag_watchdog() -> None:
    """每 60 秒看一眼。挂了就拉起来，别让 knowledge_search 静默降级。"""
    while not _RAG_WD_STOP.wait(60):
        if not _rag_alive():
            print(f"[rag] 掉线，重新拉起：{_ensure_rag()}", flush=True)


@app.on_event("startup")
def _on_startup() -> None:
    state = _ensure_rag()
    print(f"[rag] {state}", flush=True)
    _th.Thread(target=_rag_watchdog, daemon=True).start()


@app.on_event("shutdown")
def _on_shutdown() -> None:
    _RAG_WD_STOP.set()
    if _RAG_PROC is not None and _RAG_PROC.poll() is None:
        _RAG_PROC.terminate()


@app.get("/api/status")
def status():
    import os
    st = runner.status()
    # 知识库状态：离线时 knowledge_search 会静默降级成联网搜索，
    # 不显式报出来用户很难察觉自己拿的是"次优答案"。
    try:
        import httpx as _hx
        with _hx.Client(timeout=1.5) as _c:
            st["rag"] = _c.get("http://127.0.0.1:8601/health").json()
    except Exception:                                  # noqa: BLE001
        st["rag"] = {"state": "offline",
                     "detail": "知识库未启动；knowledge_search 会降级成联网搜索"}
    # 网关鉴权是否开启（配了 MODEL_STUDIO_TOKEN 才校验）
    st["auth_required"] = bool(os.environ.get("MODEL_STUDIO_TOKEN", "").strip())
    st["providers"] = [
        {"id": p.id, "name": p.name, "has_key": p.has_key, "key_source": p.key_source}
        for p in providers.load_providers()
    ]
    return st


class StartReq(BaseModel):
    model_id: str
    password: str | None = None
    port: int = Field(8080, ge=1024, le=65535)
    max_tokens: int = Field(2048, ge=64, le=32768)
    context: int = Field(32768, ge=512, le=131072)   # 仅对 GGUF 生效
    thinking: bool = False


@app.post("/api/start")
def start(req: StartReq, request: Request):
    # 启停模型是控制权，只允许本机。公网/局域网一律禁止。
    _require_local(request)
    spec = models.find(req.model_id)
    if not spec:
        raise HTTPException(404, "模型不存在，先刷新列表")
    try:
        return runner.start(spec, req.port, max_tokens=req.max_tokens,
                            context=req.context, thinking=req.thinking)
    except RuntimeError as e:
        raise HTTPException(400, str(e))


class StopReq(BaseModel):
    password: str | None = None


@app.post("/api/stop")
def stop(req: StopReq | None = None, request: Request = None):
    _require_local(request)          # 公网禁止停止模型
    return runner.stop()


@app.post("/api/mem/clean")
def mem_clean(request: Request):
    """一键清理内存：停模型 + 杀残留 mlx_lm.server，报告释放量。只允许本机。"""
    _require_local(request)
    return memclean.clean()


@app.get("/api/logs")
def logs(lines: int = 200):
    return {"path": runner.log_path(), "text": runner.tail(lines)}


# ------------------------------------------------------------------ 目标解析
class Target(BaseModel):
    url: str
    headers: dict
    model: str
    label: str
    remote: bool
    provider: str = ""      # 实际用的 provider id（""=本地），自动模式下要回传给前端
    reason: str = ""        # 自动选择的理由（人类可读）


def _auto_pick(messages: list[dict]) -> tuple[str, str]:
    """自动模式：按问题内容挑模型。规则在 app/router.py，可解释。"""
    last = ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            last = str(m.get("content") or "")
            break
    provs = {p.id: p for p in providers.load_providers()}
    return router.pick(
        last,
        local_ready=runner.status().get("state") == "ready",
        has_chat_api=bool(provs.get("api__deepseek") and provs["api__deepseek"].has_key),
        has_reason_api=bool(provs.get("api__deepseek_r1") and provs["api__deepseek_r1"].has_key),
    )


async def resolve_target(provider_id: str | None,
                         messages: list[dict] | None = None) -> Target:
    """把 provider 标识翻译成「往哪发请求」。

    provider_id 传 "auto" 时按问题内容自动选（见 app/router.py）。
    """
    reason = ""
    if provider_id == "auto":
        provider_id, reason = _auto_pick(messages or [])

    if provider_id and provider_id.startswith("api__"):
        p = providers.find(provider_id)
        if not p:
            raise HTTPException(404, f"未知 API provider：{provider_id}")
        if not p.has_key:
            raise HTTPException(400, f"{p.name} 缺少 {p.key_env}，填到 .env 或环境变量里")
        base = p.base_url.rstrip("/")
        # DeepSeek 的 base 不带 /v1，OpenAI 的带。统一处理：
        url = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
        return Target(url=url, headers=providers.auth_headers(p),
                      model=p.model, label=p.name, remote=True,
                      provider=p.id, reason=reason)

    st = runner.status()
    if st["state"] != "ready":
        # 自动拉起，而不是甩一个 409 让用户自己去点启动。
        # 冷启动要 20~40 秒（加载 + Metal 编译），客户端会等这一下。
        await ensure_local_ready()
        st = runner.status()
        if st["state"] != "ready":
            raise HTTPException(503, f"本地模型启动失败（{st['state']}）：{st.get('error','')}")
    return Target(url=f"http://127.0.0.1:{st['port']}/v1/chat/completions",
                  headers={"Content-Type": "application/json"},
                  model="default_model", label=st["model"] or "local", remote=False,
                  provider="", reason=reason)


# ------------------------------------------------------------------ 对话
class ChatReq(BaseModel):
    messages: list[dict]
    provider: str | None = None
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = Field(512, ge=1, le=8192)
    guest_notes: str | None = None   # 公网访客自己的记忆（前端 localStorage 传上来）


def _clean_visitor_msgs(msgs: list[dict]) -> list[dict]:
    """剔除历史里"无法获取身份信息"的问答对。

    访客第一次问"我是谁"时没有记忆，模型答了"无法获取身份"。
    之后他写了记忆再问，历史里那几轮会被当成范例，模型继续照抄。
    所以有记忆时把这类轮次删掉。
    """
    out = []
    i = 0
    while i < len(msgs):
        m = msgs[i]
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        if (m.get("role") == "user" and nxt and nxt.get("role") == "assistant"):
            a = (nxt.get("content") or "")
            if "无法获取" in a and "身份" in a:
                i += 2          # 跳过这一问一答
                continue
        out.append(m)
        i += 1
    return out


# ------------------------------------------------------------------ 普通聊天的轻量工具
# 普通聊天（/api/chat*）之前完全没工具，问"天气怎么样"模型只能背台词
# "我无法直接获取实时天气数据"。这里开放 5 个**只读、无副作用**的联网工具，
# 配一个最多 2 轮的工具循环。文件/命令/写操作仍然只属于 agent——
# 聊天界面不该有能力删文件、跑命令。
CHAT_TOOL_NAMES = ("get_weather", "get_exchange_rate", "web_search",
                   "fetch_url", "knowledge_search")

# persona 的 NO_TOOLS 版本写着「不能联网」，必须在这里当面更正，
# 否则模型拿着 tools schema 和 system prompt 自相矛盾，还是背"无法获取"的台词。
_CHAT_TOOLS_NOTE = """
【能力更正：以此为准】

上面那句"不能联网"说的是**模型本体**（你没有网卡）。但你现在带着工具，
所以对用户来说：**你能联网**——通过 web_search / fetch_url 去查，不是直接拨号。

你手上有 5 个只读工具：
- web_search：联网搜索。**传 2~4 个关键词，不要整句、不要只传「最近」这种泛词**。
  例：问「最近 AI 新闻」→ 搜「AI 新闻 2026年11月」。
- fetch_url：抓网页正文（搜索摘要不够用时用）
- get_weather：城市实时天气 + 未来 3 天预报
- get_exchange_rate：实时汇率
- knowledge_search：本地算法知识库（OI Wiki）

【问到能力本身时怎么办】
用户问"你能联网吗 / 能搜吗 / 会上网吗 / 能查实时信息吗"，**不要背"我无法直接访问互联网"**。
正确做法：先答"能"，然后**立刻调一次 web_search 演示**（搜个当下的话题，比如今天的科技新闻），
用真实结果证明，再补一句"文件读写和执行命令仍然做不到"。空口回答不算证明。

【用工具的规矩】
1. 用户问天气、汇率、新闻、行情时，先调工具拿真实数据再回答；查不到就直说，绝不编数字。
2. 最多 4 轮工具调用，拿到能回答的信息就**立刻收尾作答**，别一轮接一轮地搜。
3. 文件读写、执行命令、删改系统**确实做不到**，这部分照实说。
"""



def _is_capability_q(text: str) -> bool:
    """「你能联网吗 / 能上网查吗」这类**问能力本身**的问题。

    实测 30B 在这种问句上大约一半概率只答「能，让我演示…」而不真的调工具，
    甚至背回 persona 那句「我无法直接访问互联网」。所以由 harness 判定后
    在 system 里下硬指令，别指望模型自觉。
    """
    q = (text or "").strip()
    if not q or len(q) > 30:
        return False
    return any(k in q for k in ("联网", "上网", "连网", "能搜", "会搜", "网吗", "搜索吗"))


def _chat_tools() -> list[dict]:
    return [t for t in tools.TOOLS
            if t.get("function", {}).get("name") in CHAT_TOOL_NAMES]


def _route_suggest(question: str) -> dict | None:
    """用训练好的 0.5B 工具路由器（独立子进程）预测该用哪个工具。

    主程序的 .venv 没有 mlx-lm，所以走一次性子进程（加载 → 分类 → 退出）。
    失败/超时/未启用一律返回 None，绝不阻塞或拖垮 agent 主流程。
    """
    if os.environ.get("MS_ROUTER", "1") == "0":
        return None
    py = os.environ.get("MS_MLX_PY",
                        "/Users/ziqilin/Projects/qwen-local/.venv/bin/python")
    script = ROOT / "training" / "route_once.py"
    if not Path(py).exists() or not script.exists():
        return None
    try:
        r = subprocess.run([py, str(script), question],
                           capture_output=True, text=True, timeout=30,
                           cwd=str(ROOT))
        if r.returncode != 0:
            return None
        # mlx 可能往 stdout 打 warning，JSON 一定在最后几行，从后往前找
        for line in reversed(r.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None
    except Exception:                    # noqa: BLE001
        return None


_WEATHER_WORDS = ("天气", "气温", "冷不冷", "热不热", "下雨", "下雪", "降温", "几度", "温度")
_WEATHER_SKIP = ("新闻", "原理", "为什么", "代码", "算法")
_CITY_HINT = ("深圳", "中山", "北京", "上海", "广州", "成都", "杭州", "武汉", "西安",
              "南京", "珠海", "东莞", "天津", "重庆", "苏州", "厦门", "长沙", "青岛",
              "纽约", "伦敦", "东京", "新加坡", "巴黎", "柏林", "首尔", "悉尼",
              "香港", "台北", "洛杉矶", "旧金山")
_FX_CODE = {"美元": "USD", "欧元": "EUR", "日元": "JPY", "港币": "HKD", "英镑": "GBP"}
_ROUTABLE = ("查", "搜", "多少", "怎么样", "怎么", "什么", "吗", "？", "?")


def _prefetch(q: str):
    """单步、无副作用的问题：harness 先查好塞给模型，省一轮生成。

    顺序有讲究：**先规则再模型**。1.7B 路由器会把「中山天气」判成 web_search，
    规则先兜住；规则认不出的短问题才交给路由器（约 1.5s，比模型空转一轮便宜）。
    """
    s = (q or "").strip()
    if not s or len(s) > 40:
        return None

    if any(w in s for w in _WEATHER_WORDS) and not any(w in s for w in _WEATHER_SKIP):
        city = next((c for c in _CITY_HINT if c in s), None)
        if city:
            return ("get_weather", {"city": city})

    if "汇率" in s or "兑" in s:
        hit = next((k for k in _FX_CODE if k in s), None)
        if hit:
            return ("get_exchange_rate", {"base": _FX_CODE[hit], "targets": "CNY"})
        return ("get_exchange_rate", {"base": "CNY"})

    if len(s) <= 30 and any(w in s for w in _ROUTABLE):
        sug = _route_suggest(s)
        if (sug and not sug.get("need_llm_args")
                and sug.get("tool") in ("get_weather", "get_exchange_rate", "knowledge_search")):
            return (sug["tool"], sug.get("args") or {})
    return None


_TIME_QS = ("今天几号", "今天的日期", "今天日期", "今天是几号", "今天是几月几号",
            "现在几点", "几点了", "今天星期几", "星期几", "现在时间", "当前时间",
            "今天是哪一天", "今天是什么日期")


def _time_answer() -> str:
    """纯日期/时间问题：直接给系统时间，别让模型凭训练记忆猜年份。"""
    import datetime as _dt

    now = _dt.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return (f"今天是 {now.year} 年 {now.month} 月 {now.day} 日，星期{wd}，"
            f"现在是 {now:%H:%M}。")


def _is_time_q(q: str) -> bool:
    s = (q or "").strip().rstrip("？?。 ")
    return 0 < len(s) <= 12 and any(k in s for k in _TIME_QS)


def _payload(req: ChatReq, t: Target, stream: bool, include_notes: bool = True,
             visitor: bool = False, with_tools: bool = False) -> dict:
    msgs = list(req.messages)
    if visitor and req.guest_notes and req.guest_notes.strip():
        msgs = _clean_visitor_msgs(msgs)
    # 两段 system 拼一起：本机状态 + 手写记忆。
    # 只走 /api/chat*（界面自己用），/v1 是给外部 agent 的，不能污染。
    # 顺序有意义：能力边界放最前，它在约束"怎么回答"；
    # 系统状态和手写记忆是"回答时可以参考的事实"
    parts = [persona.as_system(with_tools=False, visitor=visitor)]
    if with_tools:
        parts.append(_CHAT_TOOLS_NOTE)
        _q_last = next((m.get("content") or "" for m in reversed(req.messages)
                        if m.get("role") == "user"), "")
        _pf = _prefetch(_q_last)
        if _pf:
            try:
                _pres = tools.call(_pf[0], _pf[1])
                parts.append(
                    f"【本轮已替你查好（{_pf[0]}）】\n{str(_pres)[:900]}\n\n"
                    "直接把上面的真实数据写进回答；除非确实缺信息，不要再调用工具。")
            except Exception:                            # noqa: BLE001
                pass
        if _is_capability_q(_q_last):
            # 不指望模型自己演示：实测它有一半概率只回「让我演示一下」就完事；
            # 而「今天的科技新闻」这种带时间词的查询后端经常 0 结果，
            # 模型会把搜索结果里的示例文案当成真结果复述。所以这里：
            # 逐条试到有结果为止，实在不行抓一个稳定页面兜底。
            _demo, _used = "", ""
            for _q in ("科技新闻", "AI 最新动态", "人工智能 新闻"):
                try:
                    _r = str(tools.call("web_search", {"query": _q, "count": 3}))
                except Exception:                         # noqa: BLE001
                    continue
                if _r and "没搜到" not in _r and "0 结果" not in _r:
                    _demo, _used = _r, _q
                    break
            if not _demo:
                try:
                    _demo = str(tools.call("fetch_url", {"url": "https://news.ycombinator.com",
                                                         "max_chars": 600}))
                    _used = "https://news.ycombinator.com"
                except Exception as e:                    # noqa: BLE001
                    _demo, _used = f"（预搜索失败：{type(e).__name__}）", "失败"
            parts.append(
                "【系统指令：本轮已替你联网搜过】用户问的是你「能不能联网」。"
                f"下面是我刚联网（{_used}）拿到的真实结果，**直接引用它作答**：\n"
                + str(_demo)[:700]
                + "\n\n回答格式：①第一句明确答「能，我能联网搜索」；"
                "②引用上面 1~2 条真实标题/来源做证据；③补一句文件读写、执行命令仍然做不到。"
                "不要再说「让我演示一下」，演示已经做完；也不要编造上面没有的内容。")
    try:
        if memory.load_settings().get("sysinfo", True):
            parts.append(sysinfo.as_system())
    except Exception:                     # noqa: BLE001
        pass
    if include_notes:
        # 本机：用后端的长期记忆
        note = memory.notes_as_system()
        if note:
            parts.append(note)
    elif req.guest_notes and req.guest_notes.strip():
        import sys as _s
        print(f"[chat] 收到访客记忆 {len(req.guest_notes)} 字符", file=_s.stderr, flush=True)
        # 公网：用访客自己存在设备上的记忆（前端 localStorage 传来）
        parts.append(
            "【访客身份资料】\n"
            "这位访客在自己的记忆里写下了关于自己的信息。"
            "当访客问及他本人（我是谁、我叫什么、我是做什么的）时，"
            "必须直接依据这段资料回答，不要回答「无法获取身份信息」或「没有权限」——"
            "这段资料就是你的依据。只有资料里确实没有相关内容时，才说「你的记忆里没有记录」。"
            "（这段资料只关于访客本人，不涉及这台机器的主人。）\n"
            "（如果之前的对话里你回答过「无法获取身份信息」，那是在没有这份资料时说的，"
            "现在有了资料，请以资料为准重新回答。）\n"
            + req.guest_notes.strip()[:2000])
    import datetime as _dt
    _now = _dt.datetime.now()
    _wd = "一二三四五六日"[_now.weekday()]
    parts.insert(1 if parts else 0,
                 f"【当前时间】{_now:%Y-%m-%d} 星期{_wd} {_now:%H:%M}"
                 "（本机真实时间，回答日期/星期/时间一律以此为准）")
    parts = [p for p in parts if p]
    if parts and not any(m.get("role") == "system" for m in msgs):
        msgs = [{"role": "system", "content": "\n\n".join(parts)}] + msgs
    p = {
        "model": t.model,
        "messages": msgs,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "max_tokens": req.max_tokens,
        "stream": stream,
    }
    if stream:
        # 让云端在最后一帧回 usage，否则拿不到 token 数
        p["stream_options"] = {"include_usage": True}
    if with_tools:
        p["tools"] = _chat_tools()
    return p


@app.post("/api/chat")
async def chat(req: ChatReq, request: Request):
    """非流式聊天。**和流式一样挂联网工具**——之前这条路径完全没工具，
    任何走 /api/chat 的客户端问天气/新闻，模型只会背「我无法联网」。
    """
    t = await resolve_target(req.provider, req.messages)
    t0 = time.time()
    _qlast = next((m.get("content") or "" for m in reversed(req.messages or [])
                   if m.get("role") == "user"), "")
    if _is_time_q(_qlast):
        return {"content": _time_answer(), "reasoning": "", "usage": {},
                "elapsed_s": 0.0, "tok_per_s": 0.0,
                "provider": t.label, "remote": t.remote}
    if not _qlast.strip():
        # 空/纯空白消息不去打扰模型：实测它会自问自答"我能联网"，还去搜一次
        # 拿到空结果，白烧十几秒。
        return {"content": "你想问点什么？直接说就行，比如「深圳天气」「最近 AI 新闻」。",
                "reasoning": "", "usage": {}, "elapsed_s": 0.0, "tok_per_s": 0.0,
                "provider": t.label, "remote": t.remote}
    local = _is_local_request(request)
    body = _payload(req, t, False, include_notes=local, visitor=not local,
                    with_tools=not t.remote)
    rounds = 4 if body.get("tools") else 0
    seen: set[str] = set()
    d: dict = {}
    async with httpx.AsyncClient(timeout=600) as c:
        for rnd in range(rounds + 1):
            if rnd == rounds and body.get("tools"):
                body.pop("tools", None)
                body["messages"].append({
                    "role": "system",
                    "content": "工具轮次已用完。不要再调用工具，直接根据已获得的信息回答用户。"})
            r = await c.post(t.url, json=body, headers=t.headers)
            if r.status_code >= 400:
                raise HTTPException(r.status_code, f"{t.label}: {r.text[:300]}")
            d = r.json()
            msg = d["choices"][0]["message"]
            calls = msg.get("tool_calls") or []
            if not calls or not body.get("tools"):
                break
            body["messages"].append({
                "role": "assistant", "content": msg.get("content") or "",
                "tool_calls": calls})
            for tc in calls:
                nm = tc["function"]["name"]
                try:
                    ar = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    ar = {}
                sig = nm + "|" + json.dumps(ar, sort_keys=True, ensure_ascii=False)
                if nm not in CHAT_TOOL_NAMES:
                    result = ("[错误] 普通聊天只能调用："
                              + ", ".join(CHAT_TOOL_NAMES))
                elif sig in seen:
                    result = ("[重复调用] 同样的工具+参数已经执行过，结果见上文。"
                              "换个关键词，或直接作答。")
                else:
                    seen.add(sig)
                    try:
                        result = await asyncio.to_thread(tools.call, nm, ar)
                    except Exception as te:               # noqa: BLE001
                        result = f"[工具执行失败] {type(te).__name__}: {te}"
                body["messages"].append({
                    "role": "tool", "tool_call_id": tc.get("id", ""),
                    "content": str(result)[:4000]})
    usage = d.get("usage", {}) or {}
    dt = time.time() - t0
    msg = d["choices"][0]["message"]
    return {
        "content": msg.get("content") or "",
        "reasoning": msg.get("reasoning") or "",
        "usage": usage,
        "elapsed_s": round(dt, 2),
        "tok_per_s": round(usage.get("completion_tokens", 0) / max(dt, 1e-6), 1),
        "provider": t.label,
        "remote": t.remote,
    }


@app.post("/api/chat/stream")
async def chat_stream(req: ChatReq, request: Request):
    t = await resolve_target(req.provider, req.messages)

    # 问题取最后一条 user；答案在流式过程中累积，结束才落盘
    _q = ""
    for _m in reversed(req.messages or []):
        if _m.get("role") == "user":
            _q = _m.get("content") or ""
            break
    _acc: list[str] = []

    async def gen():
        t0 = time.time()
        first = None
        if _is_time_q(_q):
            # 纯日期/时间问题：直接给系统时间，别让模型猜年份（实测答成 2023）
            yield "data: " + json.dumps({"t": _time_answer()}, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps(
                {"done": True, "ttft_s": 0, "elapsed_s": 0, "chunks": 1,
                 "chunk_per_s": 0, "completion_tokens": 0,
                 "provider": t.label, "remote": t.remote}, ensure_ascii=False) + "\n\n"
            return
        if not _q.strip():
            # 空消息：直接给一句提示，别让模型自问自答（还会白搜一次）
            yield "data: " + json.dumps(
                {"t": "你想问点什么？直接说就行，比如「深圳天气」「最近 AI 新闻」。"},
                ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps(
                {"done": True, "ttft_s": 0, "elapsed_s": 0, "chunks": 1,
                 "chunk_per_s": 0, "completion_tokens": 0,
                 "provider": t.label, "remote": t.remote}, ensure_ascii=False) + "\n\n"
            return
        if req.provider == "auto":
            yield "data: " + json.dumps(
                {"route": {"provider": t.provider, "label": t.label,
                           "reason": t.reason, "remote": t.remote}},
                ensure_ascii=False) + "\n\n"
        n = 0
        usage: dict = {}
        try:
            async with httpx.AsyncClient(timeout=600) as c:
                body = _payload(req, t, True,
                                include_notes=_is_local_request(request),
                                visitor=not _is_local_request(request),
                                with_tools=not t.remote)
                # 本地模型最多 2 轮工具调用：第一轮拿数据，第二轮兜底。
                # 远端不挂工具——DeepSeek Reasoner 不支持 tools，传了直接 400。
                max_rounds = 4 if body.get("tools") else 0
                _seen_calls: set[str] = set()
                _fails = 0
                for _round in range(max_rounds + 1):
                    if _round == max_rounds and body.get("tools"):
                        # 最后一轮收口：收回工具，逼它用已有信息作答。
                        # 实测 30B 会一轮接一轮地搜，轮数一到就直接空回复。
                        body.pop("tools", None)
                        body["messages"].append({
                            "role": "system",
                            "content": "工具轮次已用完。不要再调用工具，直接根据已获得的"
                                       "信息回答用户；信息不足就说明缺什么。"})
                    round_text: list[str] = []
                    tool_acc: dict[int, dict] = {}
                    status = 200
                    err_body = ""
                    async with c.stream("POST", t.url, json=body,
                                        headers=t.headers) as r:
                        status = r.status_code
                        if status >= 400:
                            err_body = (await r.aread()).decode("utf-8", "replace")
                        else:
                            async for line in r.aiter_lines():
                                if not line.startswith("data: "):
                                    continue
                                raw = line[6:].strip()
                                if raw == "[DONE]":
                                    break
                                try:
                                    d = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                if d.get("usage"):
                                    usage = d["usage"]
                                delta = (d.get("choices") or [{}])[0].get("delta") or {}
                                piece = delta.get("content") or ""
                                # 推理模型（DeepSeek-R1 等）把思考过程放在 reasoning 里，
                                # 只取 content 会让界面显示空白——实测 1+1 要 438 token、
                                # 其中 400+ 都在 reasoning 里。
                                reason = delta.get("reasoning") or delta.get("reasoning_content") or ""
                                # 流式的 tool_calls 分块到达，按 index 累积（同 agent）。
                                # 必须放在下面的 continue **之前**：tool_calls 的 delta
                                # 通常 content/reasoning 全空，会被 continue 跳过——
                                # 放后面的话工具调用永远收集不到（实测踩过）。
                                for tc in (delta.get("tool_calls") or []):
                                    i = tc.get("index", 0)
                                    slot = tool_acc.setdefault(
                                        i, {"id": "", "name": "", "arguments": ""})
                                    if tc.get("id"):
                                        slot["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        slot["name"] += fn["name"]
                                    if fn.get("arguments"):
                                        slot["arguments"] += fn["arguments"]
                                if not piece and not reason:
                                    continue
                                if piece and first is None:
                                    first = time.time() - t0
                                if piece:
                                    n += 1
                                    _acc.append(piece)
                                    round_text.append(piece)
                                yield "data: " + json.dumps(
                                    {"t": piece, "r": reason, "ttft": first, "n": n},
                                    ensure_ascii=False) + "\n\n"
                        if status >= 400:
                            # generation thread died（Metal OOM）后服务不会自愈：
                            # TCP/HTTP 都活着、探活也通过，但每个请求都 404。
                            # 这里重启一次，让下一次请求能正常，并明确请用户重发。
                            _fatal = any(k in err_body.lower() for k in (
                                "generation thread died", "outofmemory",
                                "insufficient memory", "out of memory"))
                            if _fatal and not t.remote:
                                try:
                                    _st = runner.status()
                                    _spec = models.find(_st.get("model_id") or "")
                                    if _spec:
                                        import sys as _sc
                                        print("[chat] 上游 generation thread died，重启模型",
                                              file=_sc.stderr, flush=True)
                                        runner.stop(timeout=10)
                                        await asyncio.sleep(1)
                                        runner.start(_spec, port=_st.get("port") or 8080,
                                                     max_tokens=_st.get("max_tokens") or 2048,
                                                     thinking=False)
                                        yield "data: " + json.dumps(
                                            {"error": "模型显存不足，已自动重启恢复，请重发一次刚才的问题"},
                                            ensure_ascii=False) + "\n\n"
                                        return
                                except Exception as _re:      # noqa: BLE001
                                    import sys as _sce
                                    print(f"[chat] 自动重启失败: {_re}",
                                          file=_sce.stderr, flush=True)
                            yield "data: " + json.dumps(
                                {"error": f"{t.label} {status}: {err_body[:200]}"},
                                ensure_ascii=False) + "\n\n"
                            return
                    calls = [
                        {"id": v["id"] or f"call_{k}", "type": "function",
                         "function": {"name": v["name"],
                                      "arguments": v["arguments"] or "{}"}}
                        for k, v in sorted(tool_acc.items())
                    ]
                    if calls and _round < max_rounds:
                        # 模型要工具：执行后把真实结果回灌，下一轮让它组织成回答。
                        # 工具在 to_thread 里跑，别堵事件循环。
                        body["messages"].append({
                            "role": "assistant",
                            "content": "".join(round_text),
                            "tool_calls": calls})
                        for tc in calls:
                            nm = tc["function"]["name"]
                            try:
                                ar = json.loads(tc["function"].get("arguments") or "{}")
                            except json.JSONDecodeError:
                                ar = {}
                            if nm not in CHAT_TOOL_NAMES:
                                result = ("[错误] 普通聊天只能调用："
                                          + ", ".join(CHAT_TOOL_NAMES))
                            else:
                                _sig = nm + "|" + json.dumps(ar, sort_keys=True,
                                                             ensure_ascii=False)
                                if _sig in _seen_calls:
                                    result = ("[重复调用] 同样的工具+参数已经执行过，"
                                              "结果见上文。换个关键词，或直接作答。")
                                else:
                                    _seen_calls.add(_sig)
                                    try:
                                        result = await asyncio.to_thread(tools.call, nm, ar)
                                    except Exception as te:   # noqa: BLE001
                                        result = f"[工具执行失败] {type(te).__name__}: {te}"
                            _rs = str(result)
                            if (_rs.startswith("[错误]") or "[没搜到]" in _rs
                                    or "抓取失败" in _rs):
                                _fails += 1
                            body["messages"].append({
                                "role": "tool", "tool_call_id": tc["id"],
                                "content": _rs[:4000]})
                        if _fails >= 2 and body.get("tools"):
                            # 失败预算用完：收回工具逼它收尾，别再来回试十几秒
                            body.pop("tools", None)
                            body["messages"].append({
                                "role": "system",
                                "content": "已经连续 2 次工具失败。停止尝试，直接告诉用户"
                                           "哪一步拿不到、为什么，不要编造内容。"})
                        continue
                    break
            total = time.time() - t0
            # 落盘（失败不影响本次对话）
            try:
                memory.record(
                    _q, "".join(_acc),
                    model=t.label,
                    elapsed_s=total,
                    completion_tokens=usage.get("completion_tokens"),
                    ttft_s=first or 0.0,
                )
            except Exception as e:                    # noqa: BLE001
                import sys
                print(f"[memory] 写入失败: {e}", file=sys.stderr, flush=True)
            # 模型偶尔一个字都不输出。静默返回空白，用户以为卡死了。
            # 这里明确报一句，前端能提示重试。
            if not _acc:
                yield "data: " + json.dumps(
                    {"error": "模型没有输出内容（可能被截断或空回复），请重发一次"},
                    ensure_ascii=False) + "\n\n"
                return
            yield "data: " + json.dumps({
                "done": True,
                "ttft_s": round(first or 0, 2),
                "elapsed_s": round(total, 2),
                "chunks": n,
                "chunk_per_s": round(n / max(total, 1e-6), 1),
                "completion_tokens": usage.get("completion_tokens"),
                "provider": t.label,
                "remote": t.remote,
            }, ensure_ascii=False) + "\n\n"
        except httpx.HTTPError as e:
            yield "data: " + json.dumps({"error": f"{type(e).__name__}: {e}"},
                                        ensure_ascii=False) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ OpenAI 兼容网关
# 给外部 agent 用。和上面的 /api/* 是两套：/api 是界面自己用，
# /v1 是标准 OpenAI 协议，第三方框架直接指过来就能用。
@app.get("/v1/models")
def v1_models(request: Request):
    gateway.check_auth(request)
    return gateway.available_models()


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request):
    return await gateway.chat_completions(request)


app.include_router(rag.router)






# ------------------------------------------------------------------ Agent（带文件工具）
import uuid as _uuid

_CANCEL: dict = {}          # request_id -> asyncio.Event，任务取消用
MAX_AGENT_TURNS = 4          # 最多几轮工具调用，防止模型绕圈
TOOL_RESULT_LIMIT = 3000     # 单次工具结果最多回灌多少字符



class CancelReq(BaseModel):
    request_id: str


@app.post("/api/agent/cancel")
async def agent_cancel(req: CancelReq):
    """取消一个正在跑的 agent 任务。"""
    ev = _CANCEL.get(req.request_id)
    if not ev:
        return {"ok": False, "detail": "任务不存在或已结束"}
    ev.set()
    return {"ok": True}


class WriteProtectReq(BaseModel):
    on: bool


@app.get("/api/tools/write-protect")
def get_write_protect():
    return {"on": tools.WRITE_PROTECT}


@app.post("/api/tools/write-protect")
def set_write_protect(req: WriteProtectReq):
    tools.WRITE_PROTECT = bool(req.on)
    return {"on": tools.WRITE_PROTECT}


@app.get("/api/agent/sessions")
def agent_sessions(limit: int = 20):
    """最近的 agent 会话存档（按时间倒序）。"""
    import pathlib as _pl
    d = _pl.Path("data/agent_sessions")
    if not d.exists():
        return {"count": 0, "sessions": []}
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:max(1, min(limit, 100))]:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return {"count": len(out), "sessions": out}

@app.post("/api/agent/stream")
async def agent_stream(req: ChatReq, request: Request):
    """带文件工具的 agent。状态管理、计划注入、卡住检测都在 app/agent.py。"""
    t = await resolve_target(req.provider, req.messages)

    async def sse(obj: dict) -> str:
        return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"

    async def fin(obj: dict):
        """收尾：发最后一帧 + [DONE]。
        不发 [DONE] 的话客户端会一直等，或者在服务端关连接后报
        RemoteProtocolError——SSE 的结束标记不是可选项。"""
        yield await sse(obj)
        yield "data: [DONE]\n\n"

    req_id = _uuid.uuid4().hex[:8]
    _CANCEL[req_id] = asyncio.Event()
    # 开局就把 id 推给前端，前端拿它来取消
    async def gen():
        yield "data: " + json.dumps({"id": req_id}, ensure_ascii=False) + "\n\n"
        # 自动模式：把"选了谁、为什么"先告诉前端，用户能看懂也能手动覆盖
        if req.provider == "auto":
            yield "data: " + json.dumps(
                {"route": {"provider": t.provider, "label": t.label,
                           "reason": t.reason, "remote": t.remote}},
                ensure_ascii=False) + "\n\n"
        # 每个任务独立计数：空回复重试 / 上下文压缩 / 模型重启
        globals()["_empty_retry"] = 0
        globals()["_shrink_n"] = 0
        globals()["_restart_n"] = 0
        globals()["_garbage_retry"] = 0
        globals()["_refusal_retry"] = 0
        _visitor = not _is_local_request(request)
        parts = [persona.as_system(with_tools=True, visitor=_visitor)]
        try:
            if memory.load_settings().get("sysinfo", True):
                parts.append(sysinfo.as_system())
        except Exception:                 # noqa: BLE001
            pass
        # 本机注入主人的长期记忆；公网注入访客自己存在设备上的记忆。
        # 两者互不可见——主人记忆永远不进公网对话。
        if not _visitor:
            note = memory.notes_as_system()
            if note:
                parts.append(note)
        elif req.guest_notes and req.guest_notes.strip():
            import sys as _s2
            print(f"[chat] 收到访客记忆 {len(req.guest_notes)} 字符", file=_s2.stderr, flush=True)
            parts.append(
                "【访客身份资料】\n"
                "这位访客在自己的记忆里写下了关于自己的信息。"
                "当访客问及他本人（我是谁、我叫什么、我是做什么的）时，"
                "必须直接依据这段资料回答，不要回答「无法获取身份信息」或「没有权限」——"
                "这段资料就是你的依据。只有资料里确实没有相关内容时，才说「你的记忆里没有记录」。"
                "（这段资料只关于访客本人，不涉及这台机器的主人。）\n"
                + req.guest_notes.strip()[:2000])
        if not req.guest_notes or not req.guest_notes.strip():
            import sys as _s3
            print(f"[chat] 无访客记忆（visitor={_visitor}）", file=_s3.stderr, flush=True)
        base_system = "\n\n".join([x for x in parts if x])

        goal = ""
        for m in reversed(req.messages or []):
            if m.get("role") == "user":
                goal = m.get("content") or ""
                break
        _tidy = any(w in goal for w in ("整理", "列出", "汇总", "所有", "清单", "归纳"))
        if _tidy:
            base_system += ("\n\n【任务类型：整理/列举】用 markdown 表格归纳，配权威链接；"
                            "不要声称穷举，也不要调用 make_chart 画图。")
            # 这类任务让模型自己搜，会一路搜到 12 轮用尽还答不出（实测 5 分钟空手）。
            # harness 先把两路资料抓回来塞进 system，模型只剩"组织答案"。
            try:
                _kb = str(tools.call("knowledge_search", {"query": goal[:40], "top_k": 3}))
                _ws = str(tools.call("web_search", {"query": goal[:40], "count": 3}))
                base_system += ("\n\n【已替你检索到的资料（优先用这些，最多再补 1 次搜索）】\n"
                                + _kb[:900] + "\n---\n" + _ws[:900])
            except Exception:                     # noqa: BLE001
                pass
        state = agent.AgentState(goal=goal[:200])

        # 训练好的 0.5B 工具路由器先判一下「大概率要哪个工具」，作为提示。
        # 只在本地模型 + 问题疑似要用工具时调（纯寒暄/知识问答跳过，省 12 秒）。
        # 结果只是参考，30B 自己决定；失败/超时静默降级，绝不阻塞主流程。
        if (not t.remote and os.environ.get("MS_ROUTER", "1") != "0"
                and agent.looks_like_tool_intent(goal)):
            try:
                _hint = await asyncio.to_thread(_route_suggest, goal)
            except Exception:                       # noqa: BLE001
                _hint = None
            if _hint and _hint.get("tool"):
                _tool = _hint["tool"]
                _args = _hint.get("args") or {}
                _hint_text = ("【工具路由提示】一个小路由器判断这个问题最可能需要"
                              f" `{_tool}`")
                if _args:
                    _hint_text += f"，它初步抽到的参数是 {_args}"
                _hint_text += "。仅作参考，你自己解析后再调用工具（参数以你为准）。"
                parts.append(_hint_text)
                base_system = "\n\n".join([x for x in parts if x])

        _raw = list(req.messages)
        if _visitor and req.guest_notes and req.guest_notes.strip():
            _raw = _clean_visitor_msgs(_raw)
        msgs = [{"role": "system", "content": base_system}] + _raw
        t0 = time.time()
        toolset = agent.all_tools(tools.TOOLS)

        try:
            async with httpx.AsyncClient(timeout=600) as c:
                # 用 while 而不是 for：空回复重试和 OOM 自动重启不该吃掉轮次。
                # 之前 for 循环里 continue 一次就少一轮，两次重启 = 白丢两轮。
                turn = 0
                _t_start = time.time()
                _force_final = False
                while turn < agent.MAX_TURNS:
                    turn += 1
                    if not _force_final and time.time() - _t_start > 180:
                        # 止损：实测这类"整理"任务能烧到 5 分钟，必须有个兜底
                        _force_final = True
                        base_system += ("\n\n【时间到】已经检索很久了，不要再调用工具，"
                                        "立刻用现有信息和你的知识给出最终答案。")
                    if _CANCEL[req_id].is_set():
                        async for x in fin({"error": "任务已取消"}):
                            yield x
                        return
                    # 每轮重新注入进度：模型才知道自己在第几步
                    msgs[0]["content"] = agent.build_system(base_system, state)
                    saved = agent.compress(msgs)
                    if saved:
                        import sys as _s2
                        print(f"[agent] 压缩上下文 {saved} 字符", file=_s2.stderr, flush=True)
                    body = {
                        "model": t.model,
                        "messages": msgs,
                        "temperature": req.temperature,
                        "max_tokens": req.max_tokens,
                    }
                    if not _force_final:
                        body["tools"] = toolset
                    # 流式调上游：文字边收边推给前端，工具调用边收边累积。
                    # 非流式的话用户要干等 20-40 秒才看到任何东西。
                    body["stream"] = True
                    acc_text: list[str] = []
                    tool_acc: dict[int, dict] = {}
                    status = 200
                    err_body = ""
                    async with c.stream("POST", t.url, json=body,
                                        headers=t.headers) as r:
                        status = r.status_code
                        if status >= 400:
                            err_body = (await r.aread()).decode("utf-8", "replace")
                        else:
                            async for line in r.aiter_lines():
                                if _CANCEL[req_id].is_set():
                                    async for x in fin({"error": "任务已取消"}):
                                        yield x
                                    return
                                if not line.startswith("data: "):
                                    continue
                                raw = line[6:].strip()
                                if raw == "[DONE]":
                                    break
                                try:
                                    d = json.loads(raw)
                                except json.JSONDecodeError:
                                    continue
                                ch = (d.get("choices") or [{}])[0]
                                delta = ch.get("delta") or {}
                                piece = delta.get("content") or ""
                                if piece:
                                    acc_text.append(piece)
                                    yield await sse({"t": piece})
                                # 流式的 tool_calls 是分块的，按 index 累积
                                for tc in (delta.get("tool_calls") or []):
                                    i = tc.get("index", 0)
                                    slot = tool_acc.setdefault(
                                        i, {"id": "", "name": "", "arguments": ""})
                                    if tc.get("id"):
                                        slot["id"] = tc["id"]
                                    fn = tc.get("function") or {}
                                    if fn.get("name"):
                                        slot["name"] += fn["name"]
                                    if fn.get("arguments"):
                                        slot["arguments"] += fn["arguments"]

                    if status >= 400:
                        # mlx_lm.server 的 generation thread 崩掉后**不会自愈**：
                        # HTTP 还在、/v1/models 还 200、TCP 也通，但每个请求都
                        # 回 404 + {"error": "generation thread died"}（底层是
                        # Metal OutOfMemory）。空回复分支那套「探活→压缩上下文」
                        # 救不了它——探活只看 TCP，会误判成进程活着，于是白压缩、
                        # 白重试，用户看到的就是连环空回复/反复重启。
                        # 这里按错误文本精确识别，重启模型进程一次再重试本轮。
                        _fatal = any(k in err_body.lower() for k in (
                            "generation thread died", "outofmemory",
                            "insufficient memory", "out of memory"))
                        if (_fatal and not t.remote
                                and globals().get("_restart_n", 0) < 1):
                            try:
                                _st = runner.status()
                                _spec = models.find(_st.get("model_id") or "")
                                if _spec:
                                    globals()["_restart_n"] = 1
                                    yield "data: " + json.dumps(
                                        {"step": {"tool": "重启模型", "args": {},
                                                  "preview": "模型生成线程崩溃（显存不足），"
                                                             "自动重启一次后继续…"}},
                                        ensure_ascii=False) + "\n\n"
                                    import sys as _sf
                                    print("[agent] 上游 generation thread died，重启模型重试",
                                          file=_sf.stderr, flush=True)
                                    runner.stop(timeout=10)
                                    await asyncio.sleep(1)
                                    runner.start(_spec, port=_st.get("port") or 8080,
                                                 max_tokens=_st.get("max_tokens") or 2048,
                                                 thinking=False)
                                    for _ in range(60):
                                        if runner.status().get("state") == "ready":
                                            break
                                        await asyncio.sleep(1)
                                    globals()["_empty_retry"] = 0
                                    turn -= 1     # 重启不吃轮次
                                    continue
                            except Exception as _re:      # noqa: BLE001
                                import sys as _sfe
                                print(f"[agent] 崩溃后自动重启失败: {_re}",
                                      file=_sfe.stderr, flush=True)
                        async for x in fin({"error": f"{t.label}: {err_body[:200]}"}):
                            yield x
                        return

                    msg = {"content": "".join(acc_text),
                           "tool_calls": [
                               {"id": v["id"] or f"call_{k}", "type": "function",
                                "function": {"name": v["name"],
                                             "arguments": v["arguments"] or "{}"}}
                               for k, v in sorted(tool_acc.items())
                           ]}
                    calls = msg["tool_calls"]

                    if not calls:
                        _ans = (msg.get("content") or "").strip()
                        # 裸 JSON/XML 冒充工具结果（编造天气、编造搜索结果）：
                        # 打回一次，明确要求调工具。只给一次机会，避免死循环。
                        if (_ans and agent.looks_like_raw_payload(_ans)
                                and not globals().get("_garbage_retry")
                                and turn < agent.MAX_TURNS - 1):
                            globals()["_garbage_retry"] = 1
                            import sys as _sg
                            print("[agent] 检测到裸数据回答，要求改用工具重试",
                                  file=_sg.stderr, flush=True)
                            msgs.append({"role": "assistant", "content": _ans})
                            msgs.append({"role": "user", "content":
                                         "（系统提示）上面那段 JSON/XML 是你编的，"
                                         "不是工具返回的真实数据。请调用合适的工具"
                                         "（例如 get_weather / web_search / fetch_url）"
                                         "拿真实结果再回答；如果确实拿不到，就直说拿不到，"
                                         "不要输出代码块。"})
                            turn -= 1
                            continue
                        # 拒绝式回答：该调工具却直接说"我无法获取/做不到"。
                        # 只打回一次，且用户意图必须指向有工具能干的活，
                        # 避免把"我不能飞"这类正确拒绝也误判。
                        _q_text = ""
                        for _m in reversed(msgs):
                            if _m.get("role") == "user":
                                _q_text = _m.get("content") or ""
                                break
                        if (_ans and not globals().get("_refusal_retry")
                                and turn < agent.MAX_TURNS - 1
                                and agent.looks_like_refusal(_ans, _q_text)):
                            globals()["_refusal_retry"] = 1
                            import sys as _sr
                            print("[agent] 检测到拒绝式回答，要求改用工具重试",
                                  file=_sr.stderr, flush=True)
                            msgs.append({"role": "assistant", "content": _ans})
                            msgs.append({"role": "user", "content":
                                         "（系统提示）你刚才说做不到/无法获取，"
                                         "但你**有工具**：天气用 get_weather、"
                                         "汇率用 get_exchange_rate、联网用 web_search / "
                                         "fetch_url、读文件用 read_file、跑代码用 run_bash。"
                                         "先调用对应工具拿真实结果再回答；"
                                         "只有工具真的失败才可以说拿不到，并说清卡在哪一步。"})
                            turn -= 1
                            continue
                        # 空回复（模型偶尔不输出就走完）→ 重试，最多 2 次。
                        # msgs 没变，continue 等于用同样的输入再问一遍。
                        if not _ans and turn < agent.MAX_TURNS - 1:
                            _retry_n = globals().get("_empty_retry", 0) + 1
                            globals()["_empty_retry"] = _retry_n
                            if _retry_n <= 2:
                                import sys as _s
                                print(f"[agent] 空回复，重试 #{_retry_n}",
                                      file=_s.stderr, flush=True)
                                turn -= 1        # 重试不吃轮次
                                continue
                            # 连续 3 次空回复。先别急着重启——**探活**：
                            #   /v1/models 还 200 → 进程活着，问题是生成层（多半是
                            #   上下文/显存挤爆）。重启不改变 prompt，重来必然还空，
                            #   就出现了"重启 5 次"的连环循环（日志实证）。
                            #   正确做法：砍上下文 + 降输出长度，再试。
                            #   /v1/models 不通或进程死了 → 才真的需要重启。
                            globals()["_empty_retry"] = 0
                            _alive = False
                            try:
                                _st = runner.status()
                                _port = _st.get("port") or 8080
                                # 只要 TCP 能连上就算活着。/v1/models 不可靠：
                                # 本机 HF 缓存目录缺失时它返回 500，但模型其实没问题，
                                # 按 500 判死会白白重启一次。
                                import socket as _sock
                                with _sock.create_connection(("127.0.0.1", int(_port)), timeout=3):
                                    _alive = True
                            except Exception:                       # noqa: BLE001
                                _alive = False

                            if _alive:
                                # 生成层挤爆 → 硬压缩：只留 system + 最近 4 条，
                                # 工具结果砍到 400 字符，并把输出上限降一半。
                                _shrink = globals().get("_shrink_n", 0) + 1
                                globals()["_shrink_n"] = _shrink
                                if _shrink <= 3:
                                    _sys = msgs[0]
                                    _tail = [m for m in msgs[1:] if m.get("role") in ("user", "assistant")]
                                    _tail = _tail[-4:]
                                    for _m in _tail:
                                        _c = _m.get("content")
                                        if isinstance(_c, str) and len(_c) > 400:
                                            _m["content"] = _c[:400] + "…"
                                        _m.pop("tool_calls", None)
                                    msgs[:] = [_sys] + _tail
                                    req.max_tokens = max(256, int((req.max_tokens or 2048) // 2))
                                    yield await sse({"step": {
                                        "tool": "压缩上下文",
                                        "args": {"次数": _shrink, "max_tokens": req.max_tokens},
                                        "preview": "模型进程还活着（探活 200），不是 OOM。"
                                                   "已把上下文砍到最近 4 条、输出上限减半，重试。"}})
                                    import sys as _s6
                                    print(f"[agent] 探活成功，压缩上下文重试 #{_shrink}"
                                          f"（max_tokens→{req.max_tokens}）",
                                          file=_s6.stderr, flush=True)
                                    turn -= 1
                                    continue

                            # 探活失败（或压缩也救不回来）：重启，但**单任务只允许 1 次**
                            _restarts = globals().get("_restart_n", 0)
                            if _restarts >= 1:
                                async for x in fin({
                                        "done": True,
                                        "answer": agent.fallback_answer(state)
                                                  + "\n\n（模型连续无输出，已重启过一次仍未恢复，"
                                                    "先停在这里。可以点左侧「停止」再「启动」，"
                                                    "或把问题拆小一点。）",
                                        "session": req_id, "steps": state.log,
                                        "plan": state.plan, "cursor": state.cursor,
                                        "changed": agent.changed_files(state),
                                        "elapsed_s": round(time.time() - t0, 1),
                                        "turns": turn}):
                                    yield x
                                return
                            try:
                                _st = runner.status()
                                _spec = models.find(_st.get("model_id") or "")
                                if _spec:
                                    globals()["_restart_n"] = _restarts + 1
                                    yield "data: " + json.dumps(
                                        {"step": {"tool": "重启模型", "args": {},
                                                  "preview": "探活不通，模型进程可能已崩溃，重启一次…"}},
                                        ensure_ascii=False) + "\n\n"
                                    import sys as _s4
                                    print("[agent] 探活失败，重启模型（本任务第 1 次）",
                                          file=_s4.stderr, flush=True)
                                    runner.stop(timeout=10)
                                    await asyncio.sleep(1)
                                    runner.start(_spec, port=_st.get("port") or 8080,
                                                 max_tokens=_st.get("max_tokens") or 2048,
                                                 thinking=False)
                                    for _ in range(60):
                                        if runner.status().get("state") == "ready":
                                            break
                                        await asyncio.sleep(1)
                                    globals()["_restart_done"] = True
                                    turn -= 1     # 重启不吃轮次
                                    continue          # 重启完再试一次
                            except Exception as _re:      # noqa: BLE001
                                import sys as _s5
                                print(f"[agent] 自动重启失败: {_re}", file=_s5.stderr, flush=True)
                            async for x in fin({"error":
                                    "模型没有输出任何内容，自动重启也失败了。"
                                    "请在左侧模型卡片手动点「停止」再「启动」。"}):
                                yield x
                            return
                        globals()["_empty_retry"] = 0
                        if not _ans:
                            # 最后一轮给了空回复：不能让界面空白，用日志拼一份总结
                            _ans = agent.fallback_answer(state)
                        _note = agent.completion_note(state)
                        # 存档：任务结束自动留一份，能回看改了什么、当时怎么规划的
                        try:
                            import pathlib as _pl
                            _sd = _pl.Path("data/agent_sessions")
                            _sd.mkdir(parents=True, exist_ok=True)
                            (_sd / f"{req_id}.json").write_text(
                                json.dumps({
                                    "id": req_id,
                                    "goal": state.goal,
                                    "plan": state.plan,
                                    "cursor": state.cursor,
                                    "changed": agent.changed_files(state),
                                    "turns": turn + 1,
                                    "elapsed_s": round(time.time() - t0, 1),
                                    "steps": state.log,
                                }, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                        except Exception:                         # noqa: BLE001
                            pass
                        async for x in fin({
                            "done": True,
                            "answer": _ans + _note,
                            "session": req_id,
                            "steps": state.log,
                            "plan": state.plan,
                            "cursor": state.cursor,
                            "changed": agent.changed_files(state),
                            "elapsed_s": round(time.time() - t0, 1),
                            "turns": turn + 1,
                        }):
                            yield x
                        return

                    msgs.append({"role": "assistant", "content": msg.get("content") or "",
                                 "tool_calls": calls})

                    # 一轮里模型可能同时要读好几个文件。只读工具可以并行，
                    # 写操作必须顺序（后一个可能要读前一个的结果）。
                    parsed = []
                    for tc in calls:
                        nm = tc["function"]["name"]
                        try:
                            ar = json.loads(tc["function"].get("arguments") or "{}")
                        except json.JSONDecodeError:
                            ar = {}
                        parsed.append((tc, nm, ar))

                    read_items = [(tc, nm, ar) for tc, nm, ar in parsed
                                  if nm in agent.READ_ONLY_TOOLS]
                    other_items = [(tc, nm, ar) for tc, nm, ar in parsed
                                   if nm not in agent.READ_ONLY_TOOLS]

                    # 并行跑只读（多个 read_file 同时进行）
                    par_results: dict[int, str] = {}
                    if len(read_items) > 1:
                        async def _one(idx, nm, ar):
                            return idx, await asyncio.to_thread(tools.call, nm, ar)
                        done_par = await asyncio.gather(*[
                            _one(i, nm, ar) for i, (_, nm, ar) in enumerate(read_items)
                        ])
                        par_results = dict(done_par)

                    # 顺序处理：并行结果 → 写操作
                    ordered = ([(tc, nm, ar, par_results.get(i))
                                for i, (tc, nm, ar) in enumerate(read_items)]
                               + [(tc, nm, ar, None) for tc, nm, ar in other_items])

                    for tc, name, args, pre in ordered:
                        yield await sse({"step_start": {"tool": name, "args": args}})

                        if pre is not None:
                            # 已并行执行完，补上 state 更新
                            agent.record(state, name, args, pre)
                            result, stuck = pre, state.push(name, args)
                        else:
                            _blocked = guard.guard_tool(state.goal, name, args)
                            if _blocked:
                                result, stuck = _blocked, state.push(name, args)
                            else:
                                result, stuck = agent.execute(state, name, args, tools)

                        if name == "write_plan":
                            yield await sse({"plan": state.plan, "cursor": state.cursor})
                        yield await sse({"step": {"tool": name, "args": args,
                                                  "preview": result.replace("\n", " ")[:160]}})
                        if stuck:
                            # 卡住也要给东西：写存档 + 用日志拼总结，别只丢一句报错
                            try:
                                import pathlib as _pl
                                _sd = _pl.Path("data/agent_sessions")
                                _sd.mkdir(parents=True, exist_ok=True)
                                (_sd / f"{req_id}.json").write_text(
                                    json.dumps({"id": req_id, "goal": state.goal,
                                                "plan": state.plan, "cursor": state.cursor,
                                                "changed": agent.changed_files(state),
                                                "turns": turn, "stuck": True,
                                                "elapsed_s": round(time.time() - t0, 1),
                                                "steps": state.log},
                                               ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                            except Exception:                 # noqa: BLE001
                                pass
                            async for x in fin({
                                    "done": True,
                                    "answer": agent.fallback_answer(state)
                                              + "\n\n（同一步重复了 6 次，已停下。"
                                                "可以把需求换个说法，或者拆成几步分着问。）",
                                    "session": req_id, "steps": state.log,
                                    "plan": state.plan, "cursor": state.cursor,
                                    "changed": agent.changed_files(state),
                                    "elapsed_s": round(time.time() - t0, 1),
                                    "turns": turn}):
                                yield x
                            return
                        msgs.append({"role": "tool",
                                     "tool_call_id": tc.get("id") or name,
                                     "content": result[:agent.TOOL_RESULT_LIMIT]})

                # 12 轮用完：别丢一个红色报错糊弄过去，把过程和卡点交代清楚
                async for x in fin({
                        "done": True,
                        "answer": agent.fallback_answer(state)
                                  + f"\n\n（已用满 {agent.MAX_TURNS} 轮工具调用，"
                                    "任务可能太大，拆成几步分别问会更好）",
                        "session": req_id,
                        "steps": state.log,
                        "plan": state.plan,
                        "cursor": state.cursor,
                        "changed": agent.changed_files(state),
                        "elapsed_s": round(time.time() - t0, 1),
                        "turns": agent.MAX_TURNS,
                }):
                    yield x
        except Exception as e:                # noqa: BLE001
            # 必须捕获所有异常：只 catch HTTPError 的话，agent/tools 里任何一个
            # 未预期的异常（KeyError/TypeError）都会把生成器抛掉，
            # 客户端看到的是 RemoteProtocolError，没有任何错误信息。
            import sys as _s
            import traceback as _tb
            _tb.print_exc(file=_s.stderr)
            async for x in fin({"error": f"{type(e).__name__}: {e}"}):
                yield x

    async def _cleanup():
        _CANCEL.pop(req_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "X-Request-Id": req_id},
                             background=_cleanup)


@app.get("/api/memory")
def memory_stats(request: Request):
    _require_local(request)
    return memory.stats()


class MemoryPatch(BaseModel):
    """记忆设置的局部更新。字段都可选，只传要改的。"""
    enabled: bool | None = None
    sysinfo: bool | None = None
    depth: int | None = None
    max_records: int | None = None


@app.post("/api/memory")
def memory_settings(patch: MemoryPatch, request: Request):
    _require_local(request)
    saved = memory.save_settings(patch.model_dump(exclude_none=True))
    return {"settings": saved, "count": memory.stats()["count"]}



class NotesReq(BaseModel):
    text: str = ""


@app.get("/api/memory/notes")
def notes_get(request: Request):
    _require_local(request)
    return memory.load_notes()


@app.put("/api/memory/notes")
def notes_put(req: NotesReq, request: Request):
    _require_local(request)
    return memory.save_notes(req.text)

@app.get("/api/memory/history")
def memory_history(request: Request, limit: int = 50):
    _require_local(request)
    return memory.history(limit=max(1, min(limit, 500)))


@app.get("/api/memory/context")
def memory_context(depth: int = 0):
    """把最近 depth 轮转成 messages，前端拼上下文用。"""
    return {"messages": memory.context_for_model(depth)}


@app.delete("/api/memory/history")
def memory_clear(request: Request):
    _require_local(request)
    return {"deleted": memory.clear()}


# ------------------------------------------------------------------ 静态页
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

# 新版 React 前端（web/dist，`npm run build` 的产物）。构建后由 FastAPI 直接托管，
# 不需要再跑 Vite dev server；没 build 时自动回退旧单页 app.html，保证永远能打开。
WEB_DIST = ROOT / "web" / "dist"
_HAS_DIST = (WEB_DIST / "index.html").exists()
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
if _HAS_DIST:
    app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")
# 生成的图片：sandbox/generated → /generated
_GEN_DIR = tools.WRITE_ROOT / "generated"
_GEN_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/generated", StaticFiles(directory=str(_GEN_DIR)), name="generated")
_SHOT_DIR = tools.WRITE_ROOT / "shots"
_SHOT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/shots", StaticFiles(directory=str(_SHOT_DIR)), name="shots")


@app.get("/")
def index():
    # 优先新版 React 构建产物；没 build 时回退旧单页，永远能打开。
    if _HAS_DIST:
        return FileResponse(WEB_DIST / "index.html", headers=_NO_STORE)
    return FileResponse(STATIC / "app.html", headers=_NO_STORE)


@app.get("/static/client.html")
def legacy_client():
    """旧链接兼容：之前发的客户端地址指向这个路径。"""
    return FileResponse(STATIC / "app.html", headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})



class LockToggleReq(BaseModel):
    locked: bool
    password: str


class LockPasswordReq(BaseModel):
    password: str
    old_password: str | None = None


@app.get("/api/lock/status")
def lock_status():
    return lock.status()


@app.post("/api/lock/toggle")
def lock_toggle(req: LockToggleReq, request: Request):
    _require_local(request)
    ok, msg = lock.toggle(req.locked, req.password)
    if not ok:
        raise HTTPException(400, msg)
    return {"locked": req.locked, "msg": msg}


@app.post("/api/lock/password")
def lock_password(req: LockPasswordReq, request: Request):
    _require_local(request)
    ok, msg = lock.set_password(req.password, req.old_password)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg}


class GateVerifyReq(BaseModel):
    password: str


class GatePasswordReq(BaseModel):
    password: str
    old_password: str | None = None


@app.get("/api/gate/status")
def gate_status():
    return gate.status()


@app.post("/api/gate/verify")
def gate_verify(req: GateVerifyReq):
    if not gate.verify(req.password):
        raise HTTPException(401, "密码错误")
    return {"ok": True}


@app.post("/api/gate/password")
def gate_password(req: GatePasswordReq):
    ok, msg = gate.set_password(req.password, req.old_password)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg}


@app.post("/api/gate/clear")
def gate_clear():
    gate.clear()
    return {"msg": "门禁已关闭"}


# ------------------------------------------------------------------ 公网开关
TS_BIN = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


def _ts(args: list[str], timeout: int = 90) -> str:
    import subprocess
    try:
        r = subprocess.run([TS_BIN] + args, capture_output=True, text=True,
                           timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:                                # noqa: BLE001
        return f"[错误] {type(e).__name__}: {e}"


@app.get("/api/public/status")
def public_status():
    """公网（Tailscale Funnel）当前状态。"""
    out = _ts(["funnel", "status"])
    on = "Funnel on" in out
    url = ""
    import re as _re
    m = _re.search(r"https://[a-z0-9.-]+\.ts\.net", out)
    if m:
        url = m.group(0)
    return {"on": on, "url": url}


class PublicToggleReq(BaseModel):
    on: bool
    password: str | None = None


@app.post("/api/public/toggle")
def public_toggle(req: PublicToggleReq):
    """开/关公网。不需要密码——开关本身不危险，危险的是公网打开后的
    操作，那有门禁密码 + token + 模型锁三层保护。"""
    # 密码校验已移除
    out = _ts(["funnel", "--bg", "8600"] if req.on else ["funnel", "--https=443", "off"])
    return {"on": req.on, "out": out.strip()[:300]}


class TokenAddReq(BaseModel):
    name: str
    password: str


class TokenToggleReq(BaseModel):
    name_or_token: str
    enabled: bool
    password: str


class TokenRemoveReq(BaseModel):
    """删除只要名字+密码。之前复用 TokenToggleReq，enabled 是必填，
    前端删除时不发 enabled → 永远 422，token 根本删不掉。"""
    name_or_token: str
    password: str


def _require_lock(password: str):
    if lock.is_locked() and not lock.check(password or ""):
        raise HTTPException(403, "需要锁密码才能管理 token")


def _is_local_request(request: Request) -> bool:
    """是否真本机（host 在 LOCAL_HOSTS 且无 X-Forwarded-For）。"""
    host = request.client.host if request.client else ""
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    return host in LOCAL_HOSTS and not forwarded


def _require_local(request: Request):
    """控制权操作（设锁/解锁/管 token）只允许真本机。

    远程/公网（funnel 转发，带 X-Forwarded-For）即使知道密码也不能改锁，
    因为"主人"只能在 Mac 面前操作。
    """
    host = request.client.host if request.client else ""
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if host in LOCAL_HOSTS and not forwarded:
        return
    raise HTTPException(403, "此操作只能在本机（Mac）上进行")


class TokenListReq(BaseModel):
    password: str


@app.post("/api/tokens")
def tokens_list(req: TokenListReq):
    """列出附加 token（脱敏）。

    必须带锁密码：清单里有每个 token 的前缀和启用状态，
    之前裸奔（GET 无鉴权），任何人拿到网址就能看到别人有几个 token。
    """
    if not lock.has_password():
        raise HTTPException(400, "还没设置锁密码，先在顶部 🔒 里设一个")
    if not lock.check(req.password or ""):
        raise HTTPException(401, "密码错误")
    return {"master_preview": (tokens.master_token() or "")[:8] + "…",
            "tokens": tokens.list_tokens()}


@app.post("/api/tokens/add")
def tokens_add(req: TokenAddReq, request: Request):
    _require_local(request)
    _require_lock(req.password)
    ok, msg, tok = tokens.add(req.name)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg, "token": tok}


@app.post("/api/tokens/toggle")
def tokens_toggle(req: TokenToggleReq, request: Request):
    _require_local(request)
    _require_lock(req.password)
    ok, msg = tokens.toggle(req.name_or_token, req.enabled)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg}


@app.post("/api/tokens/remove")
def tokens_remove(req: TokenRemoveReq, request: Request):
    _require_local(request)
    _require_lock(req.password)
    ok, msg = tokens.remove(req.name_or_token)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg}


class LockVerifyReq(BaseModel):
    password: str


@app.post("/api/lock/verify")
def lock_verify(req: LockVerifyReq):
    """验证锁密码（只验不改状态）。供记忆界面解锁用。

    不限制本机——远程的人有正确密码也能查看记忆；没密码就看不到。
    """
    if not lock.check(req.password or ""):
        raise HTTPException(401, "密码错误")
    return {"ok": True}


@app.get("/api/preview")
def preview(path: str, request: Request):
    """把本地 HTML 文件喂给设计工作台的 iframe。

    为什么不用 file://：浏览器安全策略禁止从 http 页面加载 file:// 资源。
    """
    _require_local(request)
    from fastapi.responses import FileResponse as FR

    p = tools._resolve(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, f"文件不存在：{p}")
    if p.suffix.lower() not in {".html", ".htm", ".svg", ".css", ".js", ".md"}:
        raise HTTPException(400, "只支持 html/htm/svg/css/js/md")
    return FR(str(p), headers={"Cache-Control": "no-store"})


@app.get("/api/file-mtime")
def file_mtime(path: str, request: Request):
    """设计工作台自动刷新用：返回文件 mtime（秒）。"""
    _require_local(request)
    p = tools._resolve(path)
    if not p.exists():
        raise HTTPException(404, "文件不存在")
    return {"mtime": p.stat().st_mtime, "size": p.stat().st_size}


class DesignShotReq(BaseModel):
    path: str
    selector: str = ""
    width: int = 1440


class DesignCheckReq(BaseModel):
    path: str
    width: int = 1440


@app.post("/api/design/shot")
def design_shot(req: DesignShotReq, request: Request):
    """无头浏览器截图（整页或指定元素）。"""
    _require_local(request)
    from . import design as _design

    p = tools._resolve(req.path)
    if not p.exists():
        raise HTTPException(404, f"文件不存在：{p}")
    try:
        r = _design.shot(p, tools.WRITE_ROOT / "shots",
                         selector=req.selector, width=max(320, min(req.width, 2560)))
    except Exception as e:                                # noqa: BLE001
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise HTTPException(503, "Chromium 还没装好，运行：python -m playwright install chromium")
        raise HTTPException(500, f"{type(e).__name__}: {msg[:200]}")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "截图失败"))
    # 转成可访问的 URL
    from pathlib import Path as _P

    rel = _P(r["file"]).name
    return {"url": f"/shots/{rel}", "file": r["file"], "width": r["width"]}


@app.post("/api/design/check")
def design_check(req: DesignCheckReq, request: Request):
    """无头浏览器按真实尺寸检测布局问题。"""
    _require_local(request)
    from . import design as _design

    p = tools._resolve(req.path)
    if not p.exists():
        raise HTTPException(404, f"文件不存在：{p}")
    try:
        return _design.check(p, width=max(320, min(req.width, 2560)))
    except Exception as e:                                # noqa: BLE001
        msg = str(e)
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            raise HTTPException(503, "Chromium 还没装好，运行：python -m playwright install chromium")
        raise HTTPException(500, f"{type(e).__name__}: {msg[:200]}")


class ScheduleReq(BaseModel):
    name: str
    prompt: str
    at: str
    repeat: str = ""
    weekday: str = "0"


@app.get("/api/schedule")
def schedule_list():
    return {"schedules": scheduler.list_all()}


@app.post("/api/schedule")
def schedule_add(req: ScheduleReq):
    ok, msg, sid = scheduler.add(req.name, req.prompt, req.at, req.repeat, req.weekday)
    if not ok:
        raise HTTPException(400, msg)
    return {"msg": msg, "id": sid}


@app.delete("/api/schedule/{sid}")
def schedule_remove(sid: str):
    if not scheduler.remove(sid):
        raise HTTPException(404, "没有这个提醒")
    return {"msg": "已删除"}


@app.get("/api/schedule/notifications")
def schedule_notifications():
    return {"notifications": scheduler.notifications()}


@app.post("/api/schedule/notifications/read")
def schedule_read(req: dict):
    nid = (req or {}).get("id", "")
    scheduler.mark_read(nid)
    return {"ok": True}

@app.get("/api/health")
def health():
    return {"ok": True, "log_dir": str(LOG_DIR)}
