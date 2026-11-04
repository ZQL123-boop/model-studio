"""RAG 代理层：把 `/api/rag/*` 转发到 algo-rag 的 serve.py（8601）。

## 为什么要转发，不直接在这里跑 RAG

三个硬约束：

1. **依赖不在这里。** Model Studio 的 venv 只有 fastapi/httpx/psutil；
   RAG 要 torch、faiss、qdrant-client、sentence-transformers。
2. **加载太慢。** Searcher 要加载 bge-m3（~100 秒，含 MPS kernel 编译）。
   每次请求重启等于不可用。
3. **qdrant 本地模式不支持并发。** 两个进程同时打开 `data/qdrant` 会直接报
   `already accessed by another instance`——这也是当初 Streamlit 和它冲突的原因。

所以：algo-rag 侧起一个常驻的 `serve.py`，这里只做 HTTP 转发 + 超时控制。
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

RAG_BASE = "http://127.0.0.1:8601"

router = APIRouter(prefix="/api/rag", tags=["rag"])


class QueryReq(BaseModel):
    question: str
    top_k: int = Field(5, ge=1, le=20)
    use_rerank: bool = False


@router.get("/health")
async def health():
    """知识库加载状态。没启动时返回 offline 而不是 500，前端好判断。"""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get(f"{RAG_BASE}/health")
            return r.json()
    except httpx.HTTPError as e:
        return {
            "state": "offline",
            "detail": f"algo-rag 服务未启动。在 algo-rag 目录跑："
                      f"./.venv/bin/python serve.py 8601（原始错误 {type(e).__name__}）",
        }


@router.post("/query")
async def query(req: QueryReq):
    q = req.question.strip()
    if not q:
        raise HTTPException(400, "问题不能为空")
    # 本地模型一条要 15~60 秒，重排首次还要加载 111 秒，给足 5 分钟
    try:
        async with httpx.AsyncClient(timeout=300) as c:
            r = await c.post(f"{RAG_BASE}/query", json={
                "question": q, "top_k": req.top_k, "use_rerank": req.use_rerank,
            })
    except httpx.TimeoutException:
        raise HTTPException(504, "知识库查询超时（>300 秒）")
    except httpx.HTTPError as e:
        raise HTTPException(503, f"连不上知识库服务：{type(e).__name__}")

    if r.status_code >= 400:
        detail = "查询失败"
        try:
            detail = r.json().get("error", detail)
        except ValueError:
            detail = r.text[:200]
        raise HTTPException(r.status_code, detail)
    return r.json()

@router.get("/replay/list")
async def replay_list():
    """回放题单（129 道预跑结果的索引）。"""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{RAG_BASE}/replay/list")
            return r.json()
    except httpx.HTTPError as e:
        raise HTTPException(503, f"知识库服务未启动（{type(e).__name__}）")


@router.get("/replay/one")
async def replay_one(qid: str):
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{RAG_BASE}/replay/one", params={"qid": qid})
    except httpx.HTTPError as e:
        raise HTTPException(503, f"知识库服务未启动（{type(e).__name__}）")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:200])
    return r.json()
