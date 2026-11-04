import { useRef, useState } from "react";
import { streamSSE } from "../api";
import type { ChatMsg, GenSettings, Provider } from "../types";

interface Props {
  ready: boolean;
  providers: Provider[];
  provider: string;
  onProviderChange: (v: string) => void;
  useAgent: boolean;
  onUseAgentChange: (v: boolean) => void;
  settings: GenSettings;
  onToast: (msg: string, err?: boolean) => void;
}

export default function ChatPanel({
  ready, providers, provider, onProviderChange,
  useAgent, onUseAgentChange, settings, onToast,
}: Props) {
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);

  const scrollBottom = () => {
    requestAnimationFrame(() => {
      const el = listRef.current;
      if (el) el.scrollTop = el.scrollHeight;
    });
  };

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    if (!ready) {
      onToast("模型还没就绪，先点「启动」", true);
      return;
    }

    const history: ChatMsg[] = [...msgs, { role: "user", content: text }];
    setMsgs([...history, { role: "assistant", content: "", steps: [] }]);
    setInput("");
    setBusy(true);
    scrollBottom();

    const path = useAgent ? "/api/agent/stream" : "/api/chat/stream";
    const body = {
      messages: history.map((m) => ({ role: m.role, content: m.content })),
      provider,
      temperature: settings.temperature,
      top_p: settings.top_p,
      max_tokens: settings.max_tokens,
    };

    let acc = "";
    let route: ChatMsg["route"] = null;
    const steps: string[] = [];

    const paint = (patch: Partial<ChatMsg>) => {
      setMsgs((prev) => {
        if (prev.length === 0) return prev;
        const next = [...prev];
        next[next.length - 1] = { ...next[next.length - 1], ...patch };
        return next;
      });
    };

    try {
      for await (const f of streamSSE(path, body)) {
        if (f.error) throw new Error(f.error);
        if (f.route) route = { label: f.route.label, reason: f.route.reason };
        if (f.step_start?.tool) steps.push(f.step_start.tool);
        if (f.t) acc += f.t;
        if (f.answer) acc = f.answer; // agent 末帧是完整答案，覆盖增量
        paint({ content: acc, route, steps: [...steps] });
        scrollBottom();
      }
      if (!acc.trim()) {
        paint({ content: "（模型没有输出内容，重发试试）", failed: true });
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      paint({ content: msg, failed: true });
      onToast(msg, true);
    } finally {
      setBusy(false);
      scrollBottom();
    }
  }

  return (
    <>
      <div className="col-head">
        对话
        <span className="spacer" />
        <span>{useAgent ? "agent · 20 工具" : "普通聊天 · 5 工具"}</span>
      </div>

      <div className="msgs" ref={listRef}>
        {msgs.length === 0 && (
          <div className="empty">
            选模型 → 启动 → 提问
            <br />
            agent 模式会自动调工具（查天气、读文件、跑命令…）
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`msg ${m.role === "user" ? "user" : "bot"}`}>
            {m.role === "assistant" && m.route && (
              <div className="routechip">
                🧭 自动选型：<b>{m.route.label}</b>
                {m.route.reason ? ` · ${m.route.reason}` : ""}
              </div>
            )}
            {m.role === "assistant" && !!m.steps?.length && (
              <div className="steps">
                {m.steps.map((s, j) => (
                  <span key={j} className="step-tag">🔧 {s}</span>
                ))}
              </div>
            )}
            <div className={`bubble${m.failed ? " failed" : ""}`}>
              {m.content || (busy && i === msgs.length - 1 ? "…" : "")}
            </div>
          </div>
        ))}
      </div>

      <div className="composer">
        <div className="row">
          <textarea
            value={input}
            placeholder="问点什么…（Enter 发送 / Shift+Enter 换行）"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                send();
              }
            }}
          />
          <button className="primary" disabled={busy || !input.trim()} onClick={send}>
            {busy ? "生成中…" : "发送"}
          </button>
        </div>
        <div className="hint">
          <label style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <input
              type="checkbox"
              checked={useAgent}
              onChange={(e) => onUseAgentChange(e.target.checked)}
            />
            agent 模式
          </label>
          <select value={provider} onChange={(e) => onProviderChange(e.target.value)}>
            <option value="auto">自动选型</option>
            <option value="">本地模型</option>
            {providers.filter((p) => p.has_key).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
      </div>
    </>
  );
}
