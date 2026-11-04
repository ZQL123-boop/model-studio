"""OpenAI 兼容网关——给外部 agent 调用的统一端点。

为什么需要它：agent 框架（LangChain / LlamaIndex / AutoGen / OpenAI SDK）
只认一种协议——`POST /v1/chat/completions` + `Authorization: Bearer`。
本地 mlx_lm.server 虽然也是这个协议，但它的端口随进程生死，且只能服务一个模型。
这里做一个稳定的网关：外面永远只看到 `http://127.0.0.1:8600/v1`，
用 `model` 字段路由到本地或云端。

路由规则（按优先级）：
    "local" / "local-model"            → 当前本地模型（没跑就自动拉起）
    "<provider 的 model 名>"           → 对应云端，如 deepseek-chat
    "<provider id 去掉 api__ 前缀>"    → 同上，方便手写，如 deepseek

鉴权：设了环境变量 MODEL_STUDIO_TOKEN 就要求 `Authorization: Bearer <token>`，
没设则只靠"绑定 127.0.0.1"保护。本机自用够了，别暴露到公网。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import models, providers
from .runner import runner

LOCAL_ALIASES = {"local", "local-model", "default"}
# 本地模型的名字也直接当别名，例如 qwen3-30b-a3b
_local_names_cache: set[str] = set()


@dataclass
class Route:
    url: str
    headers: dict
    model: str
    label: str
    remote: bool


def _local_names() -> set[str]:
    """只有 mlx / gguf 算本地。ollama 的模型名不能进来。

    踩过的坑：Ollama 拉了 qwen3:8b 之后，discover() 把它也返回了，
    于是 `model: "qwen3:8b"` 被判成本地、路由到 mlx_lm.server，
    报 `generation thread died`——明明该走 Ollama 的 11434。
    """
    global _local_names_cache
    if not _local_names_cache:
        _local_names_cache = {m.name.lower() for m in models.discover()
                              if m.kind in ("mlx", "gguf")}
    return _local_names_cache


def _is_local(model: str) -> bool:
    m = (model or "").strip().lower()
    return m in LOCAL_ALIASES or m in _local_names()


def _local_route() -> Route:
    st = runner.status()
    if st["state"] != "ready":
        raise HTTPException(409, f"本地模型未就绪（{st['state']}）。"
                                 f"先 POST /api/start，或带 model=cloud 的请求")
    return Route(url=f"http://127.0.0.1:{st['port']}/v1/chat/completions",
                 headers={"Content-Type": "application/json"},
                 model="default_model", label=st["model"] or "local", remote=False)


def resolve(model: str) -> Route:
    if _is_local(model):
        return _local_route()

    m = (model or "").strip()
    for p in providers.load_providers():
        short = p.id.replace("api__", "")
        if m in (p.model, short, p.id):
            if not p.has_key:
                raise HTTPException(400, f"{p.name} 缺少 {p.key_env}")
            base = p.base_url.rstrip("/")
            url = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
            return Route(url=url, headers=providers.auth_headers(p),
                         model=p.model, label=p.name, remote=True)

    avail = available_models()
    raise HTTPException(
        404,
        f"未知模型 {model!r}。可用：{', '.join(m['id'] for m in avail['data'])}")


def available_models() -> dict:
    """OpenAI 的 /v1/models 格式。"""
    now = int(time.time())
    data = []
    local_models = models.discover()
    if local_models:
        # `local` 是所有本地模型的统一别名，只出现一次；否则有几个模型就有几个 local
        data.append({"id": "local", "object": "model", "created": now,
                     "owned_by": "local",
                     "meta": {"resolves_to": local_models[0].name,
                              "size_gb": local_models[0].size_gb}})
    for m in local_models:
        data.append({"id": m.name, "object": "model", "created": now,
                     "owned_by": "local", "meta": {"alias_of": "local"}})
    for p in providers.load_providers():
        if p.has_key:
            data.append({"id": p.model, "object": "model", "created": now,
                         "owned_by": "remote", "meta": {"provider": p.name,
                                                        "source": p.key_source}})
    return {"object": "list", "data": data}


# ------------------------------------------------------------------ 鉴权
def check_auth(request: Request) -> None:
    token = os.environ.get("MODEL_STUDIO_TOKEN", "").strip()
    if not token:
        return
    got = request.headers.get("authorization", "")
    if not got.lower().startswith("bearer "):
        raise HTTPException(401, "缺少 Authorization: Bearer <token>")
    if got[7:].strip() != token:
        raise HTTPException(401, "token 不对")


# ------------------------------------------------------------------ 自动拉起本地模型
async def ensure_local_ready(timeout: float = 180, prefer: str | None = None) -> None:
    """确保本地模型就绪。`prefer` 指定要哪个（客户端点名时）。

    这里必须只启动一次：之前 _prepare 先 runner.start() 再调本函数，
    本函数看 state 不是 ready 又 start 一次，直接 409「已有模型在运行」。
    """
    st = runner.status()
    if st["state"] == "ready" and (not prefer or st.get("model") == prefer):
        return

    cands = [m for m in models.discover() if m.ready]
    if not cands:
        raise HTTPException(503, "本机没有可启动的本地模型")
    target = next((m for m in cands if prefer and m.name.lower() == prefer.lower()), cands[0])

    need_start = (st["state"] not in ("ready", "starting")
                  or st.get("model") != target.name)
    if need_start:
        if st["state"] in ("ready", "starting"):
            runner.stop()
        import sys
        print(f"[switch] {st.get('model')} → {target.name}", file=sys.stderr, flush=True)
        try:
            # 沿用上次启动的服务端上限，别硬编码：/api/start 默认 2048，
            # 这里写死 4096 会让同一台机器两次启动参数不一致，自动重启
            # 后读到的 max_tokens 也跟着漂。
            runner.start(target, 8080, max_tokens=st.get("max_tokens") or 2048)
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    deadline = time.time() + timeout
    while time.time() < deadline:
        st = runner.status()
        if st["state"] == "ready":
            return
        if st["state"] == "error":
            raise HTTPException(500, f"本地模型启动失败：{st['error']}")
        await asyncio.sleep(1.0)
    raise HTTPException(504, "本地模型启动超时")


# ------------------------------------------------------------------ 转发
def _log_call(model: str, stream: bool, ttft: float | None, total: float,
              pt: int = 0, ct: int = 0, switched: str = "") -> None:
    """把每次 /v1 调用的关键指标打到 stderr（会被 launchd/run.sh 收进日志）。

    没有这行日志，用户说"慢"就只能靠猜——是加载慢、prefill 慢还是生成慢，
    分不清就没法优化。
    """
    import sys
    bits = [f"model={model}", f"stream={int(stream)}",
            f"prompt={pt}", f"gen={ct}", f"total={total:.1f}s"]
    if ttft is not None:
        bits.append(f"ttft={ttft:.1f}s")
        bits.append(f"gen_speed={ct / max(total - ttft, 0.01):.1f}tok/s")
    if switched:
        bits.append(f"switched_from={switched}")
    print("[v1] " + "  ".join(bits), file=sys.stderr, flush=True)


async def _auto_restart_local(reason: str = "") -> None:
    """mlx_lm.server 的生成线程会莫名死掉：进程还占着端口，但每个请求都返回
    `{"error": "generation thread died"}`。不重启的话客户端永远转圈。

    实测触发场景：与 Ollama 抢内存之后、长时间闲置后。
    """
    import sys
    # 必须记住崩溃前跑的是哪个模型。不传 prefer 的话 ensure_local_ready 会按
    # 体积降序挑最大的（30B），于是 R1 崩了重启成 30B，30B 再崩，死循环。
    prev = runner.spec.name if runner.spec else None
    print(f"[self-heal] 本地模型异常（{reason}），重启 {prev} …", file=sys.stderr, flush=True)
    try:
        runner.stop()
    except Exception as e:
        print(f"[self-heal] stop 失败: {e}", file=sys.stderr, flush=True)
    try:
        await ensure_local_ready(prefer=prev)
        print(f"[self-heal] 重启完成（{prev}）", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[self-heal] 重启失败: {e}", file=sys.stderr, flush=True)


def _is_dead_thread(text: str) -> bool:
    return "thread died" in text or "generation thread" in text


async def _prepare(request: Request) -> tuple[Route, dict]:
    check_auth(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "请求体不是合法 JSON")

    model = body.get("model") or "local"
    if _is_local(model) and body.get("auto_start", True) is not False:
        # 客户端点名了具体模型就切过去；只说 local 就用当前/默认的。
        # 不这么做的话，dsh 里选 R1-14B 实际会跑 30B（19.4GB），
        # Agent 的长 prompt 一压就 generation thread died。
        prefer = None if model.strip().lower() in LOCAL_ALIASES else model.strip()
        await ensure_local_ready(prefer=prefer)

    route = resolve(model)
    body["model"] = route.model
    body.pop("auto_start", None)
    return route, body


def _err(status: int, msg: str) -> JSONResponse:
    return JSONResponse({"error": {"message": msg, "type": "model_studio_error"}},
                        status_code=status)


async def chat_completions(request: Request):
    try:
        route, body = await _prepare(request)
    except HTTPException as e:
        return _err(e.status_code, e.detail)

    if not body.get("stream"):
        t_call = time.time()
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(route.url, json=body, headers=route.headers)
            if r.status_code >= 400 and not route.remote and _is_dead_thread(r.text):
                await _auto_restart_local(r.text[:80])   # 重启并重试，用户无感
                route = _local_route()
                r = await c.post(route.url, json=body, headers=route.headers)
            if r.status_code >= 400:
                return _err(r.status_code, f"{route.label}: {r.text[:300]}")
            data = r.json()
            u = data.get("usage") or {}
            _log_call(route.model, False, None, time.time() - t_call,
                      u.get("prompt_tokens", 0), u.get("completion_tokens", 0))
            return JSONResponse(data)

    async def gen():
        t0 = time.time()
        ttft = None
        try:
            async with httpx.AsyncClient(timeout=900) as c:
                async with c.stream("POST", route.url, json=body, headers=route.headers) as r:
                    if r.status_code >= 400:
                        raw = (await r.aread()).decode("utf-8", "replace")
                        if not route.remote and _is_dead_thread(raw):
                            # 重启后告诉客户端"重发一次"，比让它干等好
                            await _auto_restart_local(raw[:80])
                            yield "data: " + json.dumps(
                                {"error": {"message": "本地模型进程异常，已自动重启，请重新发送这条消息",
                                           "type": "model_restarted"}},
                                ensure_ascii=False) + "\n\n"
                            yield "data: [DONE]\n\n"
                            return
                        yield "data: " + json.dumps(
                            {"error": {"message": f"{route.label}: {raw[:300]}"}},
                            ensure_ascii=False) + "\n\n"
                        # 必须补 [DONE]：流式响应的 HTTP 头已经以 200 发出去了，
                        # 不发结束标记的话客户端（dsh、各种 SDK）会一直转圈等下去。
                        # 实测 dsh 发"你好"一直加载就是这个原因。
                        yield "data: [DONE]\n\n"
                        return
                    async for chunk in r.aiter_raw():
                        if ttft is None and chunk:
                            # 首字节 ≈ 冷启动加载 + prefill。和 total 一比
                            # 就知道瓶颈在输入端还是生成端。
                            ttft = time.time() - t0
                        yield chunk
        except httpx.HTTPError as e:
            # 上游中途断连（模型崩溃、OOM）也要给客户端一个明确的收尾
            yield "data: " + json.dumps(
                {"error": {"message": f"{route.label}: 上游断开 {type(e).__name__}"}},
                ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
        finally:
            _log_call(route.model, True, ttft, time.time() - t0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
