import type { LocalModel, ModelStatus } from "../types";

interface Props {
  models: LocalModel[];
  status: ModelStatus | null;
  selected: LocalModel | null;
  busy: boolean;
  onSelect: (m: LocalModel) => void;
  onStart: () => void;
  onStop: () => void;
  onClean: () => void;
}

export default function ModelList({
  models, status, selected, busy, onSelect, onStart, onStop, onClean,
}: Props) {
  const running = status?.state === "ready" || status?.state === "starting";
  return (
    <>
      <div className="col-head">
        <span>本地模型</span>
        <span className="spacer" />
        <span>{models.length}</span>
      </div>
      <div className="scroll">
        {models.length === 0 && (
          <div className="empty">没有发现模型<br />检查 ~/Projects/*/models</div>
        )}
        {models.map((m) => (
          <div
            key={m.id}
            className={
              "model" +
              (selected?.id === m.id ? " sel" : "") +
              (status?.model === m.name ? " running" : "")
            }
            onClick={() => onSelect(m)}
            title={m.path}
          >
            <div className="name">{m.name}</div>
            <div className="meta">
              <span>{m.kind.toUpperCase()}</span>
              <span>{m.size_gb.toFixed(1)} GB</span>
              {m.ready ? (
                <span style={{ color: "var(--green)" }}>可启动</span>
              ) : (
                <span>不可用</span>
              )}
            </div>
            {m.note && <div className="note">{m.note}</div>}
          </div>
        ))}
      </div>
      <div className="actions">
        <div className="row">
          <button
            className="primary"
            disabled={!!running || !selected?.ready || busy}
            onClick={onStart}
          >
            {running ? "运行中" : "启动"}
          </button>
          <button className="danger" disabled={!running || busy} onClick={onStop}>
            停止
          </button>
        </div>
        <button disabled={busy} onClick={onClean}>
          🧹 一键清理内存
        </button>
        <div className="hint">
          {running ? "同一时刻只能跑一个本地模型" : "启动约需 10~30 秒（加载权重）"}
        </div>
      </div>
    </>
  );
}
