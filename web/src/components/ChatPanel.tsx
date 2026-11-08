import {
  useEffect, useRef, useState,
  type Dispatch, type SetStateAction, type ReactNode,
} from "react";
import { streamSSE } from "../api";
import { getToken, isLocalHost } from "../auth";
import type { ChatMsg, GenSettings, Provider, TraceItem } from "../types";

function isAbortError(e: unknown): boolean {
  return e instanceof Error && e.name === "AbortError";
}

// generate_image 返回的是 Markdown 图片语法：![描述](/generated/xxx.jpg)。
// 气泡原来直接把 content 当纯文本塞进去，这行会原样显示成字符串。
// 只解析图片这一种语法，不做全量 Markdown：不用引依赖，也不扩大 XSS 面。
// URL 只放行同源静态路径和 http(s)，模型塞 javascript: / data: 一律不渲染。
const MD_IMG = /!\[([^\]]*)\]\(([^)\s]+)\)/g;

function safeImgSrc(u: string): string | null {
  const s = u.trim();
  if (/^https?:\/\//i.test(s)) return s;
  if (/^\/(generated|shots|static|assets)\//.test(s)) return s;
  return null;
}

function MessageBody({ text }: { text: string }) {
  const parts: ReactNode[] = [];
  let last = 0;
  MD_IMG.lastIndex = 0;            // 带 g 的正则要重置，否则多次渲染会串位
  let m: RegExpExecArray | null;
  while ((m = MD_IMG.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const src = safeImgSrc(m[2]);
    parts.push(src
      ? <img key={m.index} className="md-img" src={src} alt={m[1] || "生成的图片"} />
      : m[0]);                    // 不安全 URL 保留原文，别静默吞掉
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return <>{parts}</>;
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
  // 每次工具调用上报一条 trace（给「追踪」面板）
  onTrace: (t: TraceItem) => void;
}

export default function ChatPanel({
  ready, providers, provider, onProviderChange,
  useAgent, onUseAgentChange, settings, onToast,
  msgs, setMsgs, onTrace,
}: Props) {
  // 远程也能用 agent，但后端只给它只读工具（不能写文件 / 执行命令）
  const isLocal = isLocalHost();
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // 清空对话要二次确认，避免手滑把整段对话弄没了
  const [confirmClear, setConfirmClear] = useState(false);
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

    const path = useAgent ? "/api/agent/stream" : "/api/chat/stream";
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
        if (f.step?.tool) {
          const pv = f.step.preview || "";
          onTrace({
            id: `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
            at: Date.now() - (f.step.ms || 0),
            tool: f.step.tool,
            args: (f.step.args || {}) as Record<string, unknown>,
            preview: pv,
            ms: f.step.ms,
            ok: !/^\[(错误|拒绝|重复调用|工具执行失败)/.test(pv),
          });
        }
        if (f.t) acc += f.t;
        if (f.answer) acc = f.answer; // agent 末帧是完整答案，覆盖增量
        if (f.done) {
          // agent 生成的文件在 sandbox/generated 下，转成下载链接：
          // 远程访客点一下，文件落到他自己的设备（浏览器不能直写磁盘）
          const downloads = (f.changed || [])
            .filter((p) => p.includes("/generated/"))
            .map((p) => {
              const rel = p.slice(p.indexOf("/generated/") + "/generated/".length);
              return { name: rel.split("/").pop() || rel, url: `/generated/${rel}` };
            });
          paint({
            content: acc, route, steps: [...steps],
            ttft: f.ttft_s, elapsed: f.elapsed_s, downloads,
          });
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

  // 确认态 4 秒后自动收回，别让按钮一直停在「确认清空？」
  useEffect(() => {
    if (!confirmClear) return;
    const t = window.setTimeout(() => setConfirmClear(false), 4000);
    return () => window.clearTimeout(t);
  }, [confirmClear]);

  function clearChat() {
    if (busy) {
      onToast("先停止当前回答再清空", true);
      return;
    }
    if (!confirmClear) {
      setConfirmClear(true);
      return;
    }
    setMsgs([]);
    // App 的 useEffect 会写回 []，这里显式删一次更干净（隐私模式可能抛错）
    try { sessionStorage.removeItem("ms_chat_msgs"); } catch { /* 忽略 */ }
    setInput("");
    setConfirmClear(false);
    // 顺带重置服务端的「上一张图描述」；失败不影响前端已清空
    const t = getToken();
    void fetch("/api/agent/clear", {
      method: "POST",
      headers: t ? { Authorization: `Bearer ${t}` } : undefined,
    }).catch(() => { /* 静默：前端清空已经生效 */ });
    onToast("对话已清空");
  }

  return (
    <>
      <div className="col-head">
        对话
        <span className="spacer" />
        <span>{useAgent ? "agent · 20 工具" : "普通聊天 · 5 工具"}</span>
        <button
          className={`clear-chat${confirmClear ? " confirm" : ""}`}
          onClick={clearChat}
          title="清空当前对话（不可撤销）"
        >
          {confirmClear ? "确认清空？" : "清空对话"}
        </button>
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
              {m.content
                ? <MessageBody text={m.content} />
                : (busy && i === msgs.length - 1 ? "…" : "")}
            </div>
            {m.role === "assistant" && m.elapsed != null && (
              <div className="msg-meta">
                {m.ttft != null && <span>首 token {m.ttft.toFixed(2)}s</span>}
                <span>总耗时 {m.elapsed.toFixed(1)}s</span>
              </div>
            )}
            {m.role === "assistant" && !!m.downloads?.length && (
              <div className="msg-files">
                {m.downloads.map((d) => (
                  <a key={d.url} className="download" href={d.url} download={d.name}>
                    📥 下载 {d.name}
                  </a>
                ))}
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
            style={{ display: "flex", alignItems: "center", gap: 5 }}
            title={isLocal ? "" : "远程 agent 受限：能查资料、读文件、生成可下载的文件；不能执行命令或改已有文件"}
          >
            <input
              type="checkbox"
              checked={useAgent}
              onChange={(e) => onUseAgentChange(e.target.checked)}
            />
            agent 模式{isLocal ? "" : "（远程受限）"}
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
