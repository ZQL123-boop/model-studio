import type { ModelStatus } from "../types";

export default function StatusPanel({ status }: { status: ModelStatus | null }) {
  const s = status;
  const usedPct = s ? Math.min(100, (s.sys.used_gb / s.sys.total_gb) * 100) : 0;
  return (
    <div>
      <div className="col-head">状态</div>
      <div className="stat">
        <div className="k">系统内存（含模型权重）</div>
        <div className="v">
          {s
            ? `${s.sys.used_gb.toFixed(1)} / ${s.sys.total_gb.toFixed(1)} GB · 可用 ${s.sys.free_pct.toFixed(0)}%`
            : "—"}
        </div>
        <div className="bar">
          <i style={{ width: `${usedPct}%` }} />
        </div>
      </div>
      <div className="stat">
        <div className="k">进程 RSS / PID</div>
        <div className="v">
          {s?.pid
            ? `${s.rss_gb.toFixed(2)} GB · pid ${s.pid} · ${s.uptime_s.toFixed(0)}s`
            : "—"}
        </div>
      </div>
      <div className="stat">
        <div className="k">服务端口 / 上限</div>
        <div className="v">
          {s?.state === "ready"
            ? `127.0.0.1:${s.port} · max_tokens ${s.max_tokens ?? "—"}`
            : "—"}
        </div>
      </div>
    </div>
  );
}
