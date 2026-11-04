import { useCallback, useEffect, useState } from "react";
import { delJSON, getJSON, postJSON, putJSON } from "../api";
import type { MemoryHistory, MemoryNotes, MemoryStats } from "../types";

export default function MemoryPanel({
  onToast,
}: {
  onToast: (m: string, e?: boolean) => void;
}) {
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [notes, setNotes] = useState<MemoryNotes | null>(null);
  const [text, setText] = useState("");
  const [hist, setHist] = useState<MemoryHistory | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [s, n, h] = await Promise.all([
        getJSON<MemoryStats>("/api/memory"),
        getJSON<MemoryNotes>("/api/memory/notes"),
        getJSON<MemoryHistory>("/api/memory/history?limit=30"),
      ]);
      setStats(s);
      setNotes(n);
      setText(n.text || "");
      setHist(h);
    } catch {
      /* 后端重启中 */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function saveNotes() {
    setBusy(true);
    try {
      const n = await putJSON<MemoryNotes>("/api/memory/notes", { text });
      setNotes(n);
      onToast("长期记忆已保存");
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  }

  async function patch(p: Partial<MemoryStats["settings"]>) {
    try {
      await postJSON("/api/memory", p);
      await refresh();
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    }
  }

  async function clearHistory() {
    if (!window.confirm("清空所有历史记录？不可恢复。")) return;
    try {
      const d = await delJSON<{ deleted: number }>("/api/memory/history");
      onToast(`已删除 ${d.deleted} 条`);
      await refresh();
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    }
  }

  const s = stats?.settings;

  return (
    <>
      <div className="col-head">
        记忆
        <span className="spacer" />
        <span className="st-off">
          {stats ? `${stats.count} 条 · ${stats.size_kb} KB` : "—"}
        </span>
      </div>

      <div className="panel-body">
        <div className="mem-card">
          <div className="mem-head">
            <b>长期记忆（我手写）</b>
            <span className="mem-hint">
              {notes ? `${notes.chars} / ${notes.max_chars}` : "—"}
            </span>
          </div>
          <div className="mem-hint">
            每次对话都会作为 system 前置注入。写<b>事实和偏好</b>，别写流水账。
          </div>
          <textarea
            className="mem-notes"
            value={text}
            placeholder={"例如：\n- 我叫 boss，软件工程在读，目标是大模型应用开发\n- 回答用中文，直接给结论，不要客套"}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="mem-actions">
            <button className="primary" disabled={busy} onClick={saveNotes}>
              保存记忆
            </button>
          </div>
        </div>

        <div className="mem-card">
          <div className="mem-row">
            <label className="switch">
              <input
                type="checkbox"
                checked={!!s?.enabled}
                onChange={(e) => patch({ enabled: e.target.checked })}
              />
              <i />
            </label>
            <span>启用记忆</span>
            <span className="mem-hint">关掉后不把历史带给模型，但仍然记录</span>
          </div>

          <div className="mem-row">
            <label className="switch">
              <input
                type="checkbox"
                checked={!!s?.sysinfo}
                onChange={(e) => patch({ sysinfo: e.target.checked })}
              />
              <i />
            </label>
            <span>注入本机状态</span>
            <span className="mem-hint">内存/磁盘/CPU，约 80 token 每轮</span>
          </div>

          <div className="mem-field">
            <label>
              上下文深度 <b>{s?.depth ?? "—"}</b> 轮
            </label>
            <input
              type="range"
              min={0}
              max={20}
              step={1}
              value={s?.depth ?? 6}
              onChange={(e) => patch({ depth: Number(e.target.value) })}
            />
            <div className="mem-hint">每次发送带最近 N 轮问答。0 = 完全不带历史。</div>
          </div>

          <div className="mem-field">
            <label>
              磁盘保留上限 <b>{s?.max_records ?? "—"}</b> 条
            </label>
            <input
              type="range"
              min={50}
              max={5000}
              step={50}
              value={s?.max_records ?? 1000}
              onChange={(e) => patch({ max_records: Number(e.target.value) })}
            />
            <div className="mem-hint">超过自动删最旧的。</div>
          </div>

          <div className="mem-actions">
            <button className="danger" onClick={clearHistory}>
              清空历史
            </button>
          </div>
        </div>

        <div className="mem-list-title">
          历史记录
          <span className="spacer" />
          <span className="mem-hint">{stats?.path || ""}</span>
        </div>
        {(hist?.records || []).map((r, i) => (
          <details key={i} className="mem-item">
            <summary>
              <span className="q">{r.question}</span>
              <span className="t">{r.ts}</span>
            </summary>
            <div className="a">
              <div className="mem-ans-meta">
                {r.model} · {r.elapsed_s}s · 首 token {r.ttft_s}s
              </div>
              <div className="mem-ans">{r.answer}</div>
            </div>
          </details>
        ))}
        {hist && hist.records.length === 0 && (
          <div className="empty">还没有记录</div>
        )}
      </div>
    </>
  );
}
