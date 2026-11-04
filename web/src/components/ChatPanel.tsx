import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { streamSSE } from "../api";
import { isLocalHost } from "../auth";
import type { ChatMsg, GenSettings, Provider } from "../types";

function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

interface Props {
  ready: boolean;
  providers: Provider[];
  provider: string;
  onProviderChange: (v: string) => void;
  useAgent: boolean;
  onUseAgentChange: (v: boolean) => void;
  settings: GenSettings;
  onToast: (msg: string, err?: boolean) => void;
  // 消息提升到 App 持有：切到别的 tab 时本组件会卸载，state 放这儿会丢
  msgs: ChatMsg[];
  setMsgs: Dispatch<SetStateAction<ChatMsg[]>>;
}

export default function ChatPanel({
  ready, providers, provider, onProviderChange,
  useAgent, onUseAgentChange, settings, onToast,
  msgs, setMsgs,
}: Props) {
  // agent 有 20 个工具（含 run_bash、write_file），等于远程 shell，只允许本机跑
  const agentOk = isLocalHost();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const listRef = useRef<HTMLDivElement>(null);
  // 每次发送新建 controller；复用已 abort 的那个会让下一次请求刚发出就被取消
  const ctrlRef = useRef<AbortController | null>(null);
  const reqRef = useRef(0);

  useEffect(() => {
    // 对话 tab 卸掉时组件会卸载，生成中的流不能留在后台继续跑
    return () => {
      ctrlRef.current?.abort();
    };
  }, []);

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

    const path = useAgent && agentOk ? "/api/agent/stream" : "/api/chat/stream";
    // 只带 role/content。中断时累积的 tool 步骤留在当轮闭包里，不能混进下一次请求
    const body = {
      messages: history.map((m) => ({ role: m.role, content: m.content })),
      provider,
      temperature: settings.temperature,
      top_p: settings.top_p,
      max_tokens: settings.max_tokens,
    };

    const req = ++reqRef.current;
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;

    let acc = "";
    let route: ChatMsg["route"] = null;
    const steps: string[] = [];

    const paint = (patch: Partial<ChatMsg>) => {
      if (reqRef.current !== req) return;
      setMsgs((prev) => {
        if (prev.length === 0) return prev;
        const next = [...prev];
        next[next.length - 1] = { ...next[next.length - 1], ...patch };
        return next;
      });
    };

    try {
      for await (const f of streamSSE(path, body, ctrl.signal)) {
        if (ctrl.signal.aborted) break;
        if (f.error) throw new Error(f.error);
        if (f.route) route = { label: f.route.label, reason: f.route.reason };
        if (f.step_start?.tool) steps.push(f.step_start.tool);
        if (f.t) acc += f.t;
        if (f.answer) acc = f.answer; // agent 末帧是完整答案，覆盖增量
        if (f.done) {
          paint({ content: acc, route, steps: [...steps], ttft: f.ttft_s, elapsed: f.elapsed_s });
          scrollBottom();
          continue;
        }
        paint({ content: acc, route, steps: [...steps] });
        scrollBottom();
      }
      // 用户取消时保留已写出的字，不要换成「没有输出」
      if (ctrl.signal.aborted) return;
      if (!acc.trim()) {
        paint({ content: "（模型没有输出内容，重发试试）", failed: true });
      }
    } catch (e) {
      if (isAbortError(e) || ctrl.signal.aborted) return;
      const msg = e instanceof Error ? e.message : String(e);
      paint({ content: msg, failed: true });
      onToast(msg, true);
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null;
      // 旧请求晚到的 finally 不能把新一轮的 busy 清掉
      if (reqRef.current === req) setBusy(false);
      scrollBottom();
    }
  }

  function stop() {
    ctrlRef.current?.abort();
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
            {m.role === "assistant" && m.elapsed != null && (
              <div className="msg-meta">
                {m.ttft != null && <span>首 token {m.ttft.toFixed(2)}s</span>}
                <span>总耗时 {m.elapsed.toFixed(1)}s</span>
              </div>
            )}
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
          <button
            className="primary"
            disabled={!busy && !input.trim()}
            onClick={busy ? stop : send}
          >
            {busy ? "停止" : "发送"}
          </button>
        </div>
        <div className="hint">
          <label
            style={{ display: "flex", alignItems: "center", gap: 5, opacity: agentOk ? 1 : .45 }}
            title={agentOk ? "" : "agent 有 run_bash，等于远程 shell，只允许本机使用"}
          >
            <input
              type="checkbox"
              checked={useAgent && agentOk}
              disabled={!agentOk}
              onChange={(e) => onUseAgentChange(e.target.checked)}
            />
            agent 模式{agentOk ? "" : "（仅本机）"}
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
