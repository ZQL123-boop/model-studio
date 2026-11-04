import { useState } from "react";
import { streamSSE } from "../api";
import type { GenSettings, Provider } from "../types";

interface Side {
  text: string;
  running: boolean;
  ttft?: number;
  elapsed?: number;
  cps?: number;
  error?: string;
}

const EMPTY: Side = { text: "", running: false };

/**
 * 本地 vs 云端并排对比：同一个 prompt 同时发给两个 provider，左右分栏流式渲染。
 *
 * 注意：mlx_lm.server 同一时刻只处理一个请求，两边都选本地会串行，
 * 所以要看出差异，右边一般选云端 API。
 */
export default function ComparePanel({
  providers,
  settings,
  onToast,
}: {
  providers: Provider[];
  settings: GenSettings;
  onToast: (m: string, e?: boolean) => void;
}) {
  const cloud = providers.filter((p) => p.has_key && p.id.startsWith("api__"));
  const [prompt, setPrompt] = useState("");
  const [leftP, setLeftP] = useState("");
  const [rightP, setRightP] = useState(cloud[0]?.id ?? "");
  const [left, setLeft] = useState<Side>(EMPTY);
  const [right, setRight] = useState<Side>(EMPTY);

  const busy = left.running || right.running;

  async function consume(
    provider: string,
    messages: { role: string; content: string }[],
    set: (fn: (s: Side) => Side) => void,
  ) {
    let acc = "";
    try {
      for await (const f of streamSSE("/api/chat/stream", {
        messages,
        provider,
        temperature: settings.temperature,
        top_p: settings.top_p,
        max_tokens: settings.max_tokens,
      })) {
        if (f.error) throw new Error(f.error);
        if (f.t) {
          acc += f.t;
          set((s) => ({ ...s, text: acc }));
        }
        if (f.done) {
          set((s) => ({
            ...s,
            ttft: f.ttft_s,
            elapsed: f.elapsed_s,
            cps: f.chunk_per_s,
          }));
        }
      }
      if (!acc) set((s) => ({ ...s, error: "没有输出（模型可能未启动）" }));
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      set((s) => ({ ...s, error: m }));
      onToast(m, true);
    } finally {
      set((s) => ({ ...s, running: false }));
    }
  }

  async function go() {
    const q = prompt.trim();
    if (!q || busy) return;
    const msgs = [{ role: "user", content: q }];
    setLeft({ text: "", running: true });
    setRight({ text: "", running: true });
    await Promise.all([
      consume(leftP, msgs, setLeft),
      consume(rightP, msgs, setRight),
    ]);
  }

  return (
    <>
      <div className="col-head">
        模型对比
        <span className="spacer" />
        <span className="st-off">同一 prompt 双发，左右同时流式</span>
      </div>

      <div className="compare-grid">
        <SidePane
          title="左侧"
          provider={leftP}
          onProviderChange={setLeftP}
          providers={providers}
          side={left}
        />
        <SidePane
          title="右侧"
          provider={rightP}
          onProviderChange={setRightP}
          providers={providers}
          side={right}
        />
      </div>

      <div className="composer">
        <div className="row">
          <textarea
            value={prompt}
            placeholder="输入一个问题，两边同时回答…（Enter 发送 / Shift+Enter 换行）"
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                go();
              }
            }}
          />
          <button className="primary" disabled={busy || !prompt.trim()} onClick={go}>
            {busy ? "对比中…" : "对比"}
          </button>
        </div>
        <div className="hint">
          <span>
            本地模型同一时刻只处理一个请求，两边都选本地会串行；想看真实差异，右边选云端 API。
          </span>
        </div>
      </div>
    </>
  );
}

function SidePane({
  title,
  provider,
  onProviderChange,
  providers,
  side,
}: {
  title: string;
  provider: string;
  onProviderChange: (v: string) => void;
  providers: Provider[];
  side: Side;
}) {
  return (
    <div className="compare-col">
      <div className="compare-head">
        <span>{title}</span>
        <select value={provider} onChange={(e) => onProviderChange(e.target.value)}>
          <option value="">本地模型</option>
          <option value="auto">自动选型</option>
          {providers.filter((p) => p.has_key).map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
      </div>

      <div className="compare-body">
        {side.error ? (
          <div className="rag-err">{side.error}</div>
        ) : side.text ? (
          <div className="ans-text">{side.text}</div>
        ) : (
          <div className="empty">
            {side.running ? "生成中…" : "等待提问"}
          </div>
        )}
      </div>

      <div className="compare-stats">
        {side.ttft != null && <span>首 token <b>{side.ttft.toFixed(2)}s</b></span>}
        {side.elapsed != null && <span>总耗时 <b>{side.elapsed.toFixed(1)}s</b></span>}
        {side.cps != null && <span><b>{side.cps}</b> 块/s</span>}
      </div>
    </div>
  );
}
