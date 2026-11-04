import { useEffect, useState } from "react";
import { getJSON } from "../api";
import type { ReplayItem, ReplayOne } from "../types";

export default function ReplayPane({
  onToast,
}: {
  onToast: (m: string, e?: boolean) => void;
}) {
  const [list, setList] = useState<ReplayItem[] | null>(null);
  const [qid, setQid] = useState("");
  const [one, setOne] = useState<ReplayOne | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getJSON<{ count: number; items: ReplayItem[] }>("/api/rag/replay/list")
      .then((d) => setList(d.items || []))
      .catch((e) => {
        onToast(e instanceof Error ? e.message : String(e), true);
        setList([]);
      });
  }, [onToast]);

  async function open(id: string) {
    setQid(id);
    setBusy(true);
    try {
      setOne(await getJSON<ReplayOne>(`/api/rag/replay/one?qid=${encodeURIComponent(id)}`));
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="replay-wrap">
      <div className="replay-list">
        {list === null && <div className="replay-tip">加载题单…</div>}
        {list?.length === 0 && <div className="replay-tip">没有回放数据</div>}
        {list?.map((it) => (
          <div
            key={it.qid}
            className={`replay-row${qid === it.qid ? " on" : ""}`}
            onClick={() => open(it.qid)}
          >
            <div className="replay-q">{it.question}</div>
            <div className="replay-meta">
              <span>{it.qid}</span>
              <span>F {it.faithfulness?.toFixed(2)}</span>
              <span>R {it.answer_relevancy?.toFixed(2)}</span>
              {it.refused && <span className="badge warn">拒答</span>}
            </div>
          </div>
        ))}
      </div>

      <div className="replay-detail">
        {!one && !busy && (
          <div className="empty">
            左边选一道题
            <br />
            <span>每题都有 RAGAS 分数和检索片段</span>
          </div>
        )}
        {busy && <div className="empty">加载中…</div>}
        {one && !busy && (
          <>
            <div className="replay-head">
              <span className="mono">{one.qid}</span>
              {one.question}
            </div>
            <div className="timeline">
              <span>faithfulness <b>{one.faithfulness?.toFixed(3)}</b></span>
              <span>relevancy <b>{one.answer_relevancy?.toFixed(3)}</b></span>
              <span>耗时 <b>{Math.round(one.latency_ms || 0)}</b> ms</span>
              {!!one.rerank_ms && <span>重排 <b>{Math.round(one.rerank_ms)}</b> ms</span>}
              <span>引用 <b>{one.citations?.length || 0}</b></span>
              {one.refused && <span className="badge warn">拒答</span>}
              {one.no_citation && <span className="badge warn">无引用</span>}
            </div>

            <div className="sec-title">标准答案</div>
            <div className="ans-text">{one.gold_answer || "（无）"}</div>

            <div className="sec-title">模型答案</div>
            <div className="ans-text">{one.answer || "（无）"}</div>

            <div className="sec-title">检索片段（{one.chunks?.length || 0}）</div>
            {(one.chunks || []).map((c, i) => (
              <details key={i} className="chunk">
                <summary>
                  <b>{i + 1}.</b> {c.header_path || c.doc_id}
                  <span className="spacer" />
                  <span className="mono">{c.doc_id}</span>
                  {one.citations?.includes(c.doc_id) && (
                    <span className="badge ok">已引用</span>
                  )}
                </summary>
                <pre>{c.body}</pre>
              </details>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
