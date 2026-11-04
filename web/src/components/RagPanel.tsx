import { useEffect, useState } from "react";
import { getJSON, postJSON } from "../api";
import type { RagAnswer, RagHealth } from "../types";
import ReplayPane from "./ReplayPane";

export default function RagPanel({
  onToast,
}: {
  onToast: (m: string, e?: boolean) => void;
}) {
  const [health, setHealth] = useState<RagHealth | null>(null);
  const [q, setQ] = useState("");
  const [topK, setTopK] = useState(5);
  const [rerank, setRerank] = useState(false);
  const [busy, setBusy] = useState(false);
  const [ans, setAns] = useState<RagAnswer | null>(null);
  const [err, setErr] = useState("");
  const [wall, setWall] = useState(0);
  const [sub, setSub] = useState<"online" | "replay">("online");

  useEffect(() => {
    getJSON<RagHealth>("/api/rag/health")
      .then(setHealth)
      .catch(() => setHealth({ state: "offline" }));
  }, []);

  async function ask() {
    const question = q.trim();
    if (!question || busy) return;
    setBusy(true);
    setErr("");
    setAns(null);
    const t0 = Date.now();
    try {
      const d = await postJSON<RagAnswer>("/api/rag/query", {
        question,
        top_k: topK,
        use_rerank: rerank,
      });
      setAns(d);
      setWall((Date.now() - t0) / 1000);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m);
      onToast(m, true);
    } finally {
      setBusy(false);
    }
  }

  const cited = new Set<string>([
    ...(ans?.citations || []),
    ...(ans?.cite_index || []).map(String),
  ]);

  const ready = health?.state === "ready";

  return (
    <>
      <div className="col-head">
        知识库
        <span className="spacer" />
        <span className={ready ? "st-on" : "st-off"}>
          {ready ? "就绪" : health?.state === "offline" ? "未启动" : health?.state || "…"}
        </span>
      </div>

      <div className="subtabs">
        <span
          className={`subtab${sub === "online" ? " on" : ""}`}
          onClick={() => setSub("online")}
        >
          在线提问
        </span>
        <span
          className={`subtab${sub === "replay" ? " on" : ""}`}
          onClick={() => setSub("replay")}
        >
          回放 {health?.replay ?? 129} 题
        </span>
      </div>

      {sub === "replay" ? (
        <ReplayPane onToast={onToast} />
      ) : (
        <>
      <div className="panel-body">
        {busy && (
          <div className="empty">
            检索中…
            <br />
            <span>本地生成首次约 15~60 秒，别关页面</span>
          </div>
        )}
        {!busy && err && <div className="rag-err">{err}</div>}
        {!busy && !err && !ans && (
          <div className="empty">
            问一个算法问题
            <br />
            <span>会检索本地知识库，答案带出处标注</span>
          </div>
        )}
        {!busy && ans && (
          <div className="rag-ans">
            {ans.question && <div className="rag-q">{ans.question}</div>}
            <div className="ans-text">{ans.answer}</div>
            <div className="timeline">
              <span>检索 <b>{ans.timings?.retrieve_ms ?? "—"}</b> ms</span>
              {!!ans.timings?.rerank_ms && (
                <span>重排 <b>{ans.timings.rerank_ms}</b> ms</span>
              )}
              <span>生成 <b>{Math.round(ans.timings?.generate_ms || 0)}</b> ms</span>
              <span>端到端 <b>{wall.toFixed(1)}</b> s</span>
              {ans.cos != null && <span>余弦 <b>{ans.cos}</b></span>}
              {ans.refused && <span className="badge warn">拒答</span>}
              {ans.model && <span className="badge">{ans.model}</span>}
            </div>
            <div className="chunks-title">
              检索片段（{ans.chunks?.length || 0}）
            </div>
            {(ans.chunks || []).map((c, i) => (
              <details key={i} className="chunk">
                <summary>
                  <b>{i + 1}.</b> {c.header_path || c.doc_id}
                  <span className="spacer" />
                  <span className="mono">{(c.score ?? 0).toFixed(3)}</span>
                  {(cited.has(c.doc_id) || cited.has(String(i + 1))) && (
                    <span className="badge ok">已引用</span>
                  )}
                </summary>
                <pre>{c.body}</pre>
              </details>
            ))}
          </div>
        )}
      </div>

      <div className="composer">
        <div className="hint">
          <label className="inline">
            top_k
            <input
              type="number"
              min={1}
              max={20}
              value={topK}
              onChange={(e) => setTopK(Number(e.target.value))}
            />
          </label>
          <label className="inline">
            <input
              type="checkbox"
              checked={rerank}
              onChange={(e) => setRerank(e.target.checked)}
            />
            重排
          </label>
          <span className="spacer" />
          <span className="truncate">{health?.detail || ""}</span>
        </div>
        <div className="row">
          <textarea
            value={q}
            placeholder="例如：为什么树状数组节点管的区间长度等于 lowbit？"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                ask();
              }
            }}
          />
          <button className="primary" disabled={busy || !q.trim()} onClick={ask}>
            {busy ? "检索中…" : "提问"}
          </button>
        </div>
      </div>
        </>
      )}
    </>
  );
}
