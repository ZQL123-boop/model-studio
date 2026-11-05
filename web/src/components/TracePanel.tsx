import { useState } from "react";
import type { TraceItem } from "../types";

/**
 * 工具调用追踪：每次调用一行，点开看参数和结果。
 *
 * 为什么要这个：agent 说「完成了」的时候，用户不知道它调了几次工具、
 * 哪次慢、哪次失败重试了。LangSmith 卖的就是这个，这里做个极简版。
 */
export default function TracePanel({
  items,
  onClear,
}: {
  items: TraceItem[];
  onClear: () => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);

  const totalMs = items.reduce((s, t) => s + (t.ms || 0), 0);
  const fails = items.filter((t) => !t.ok).length;

  return (
    <>
      <div className="col-head">
        工具追踪
        <span className="spacer" />
        <span className="st-off">
          {items.length} 次调用 · 共 {totalMs} ms
          {fails > 0 && <span className="trace-bad"> · {fails} 次失败</span>}
        </span>
        {items.length > 0 && (
          <button className="mini" onClick={onClear}>清空</button>
        )}
      </div>

      <div className="trace-body">
        {items.length === 0 ? (
          <div className="empty">
            还没有工具调用。
            <br />
            用 agent 模式提一个需要动手的问题（查文件、画图、搜网页），
            这里会显示每次调用的耗时、参数和结果。
          </div>
        ) : (
          items.map((t) => (
            <div
              key={t.id}
              className={`trace-item${t.ok ? "" : " bad"}${openId === t.id ? " open" : ""}`}
              onClick={() => setOpenId(openId === t.id ? null : t.id)}
            >
              <div className="trace-row">
                <span className="trace-time">
                  {new Date(t.at).toLocaleTimeString("zh-CN", { hour12: false })}
                </span>
                <span className="trace-tool">{t.tool}</span>
                <span className="trace-ms">{t.ms != null ? `${t.ms} ms` : "—"}</span>
                <span className="trace-ok">{t.ok ? "✓" : "✗"}</span>
              </div>
              {openId === t.id && (
                <div className="trace-detail">
                  <div className="trace-label">参数</div>
                  <pre>{JSON.stringify(t.args, null, 2)}</pre>
                  <div className="trace-label">结果</div>
                  <pre>{t.preview || "（空）"}</pre>
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </>
  );
}
