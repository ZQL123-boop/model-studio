import { useState } from "react";
import { postJSON } from "../api";
import type { DesignCheck, DesignShot } from "../types";

const WIDTHS = [
  { v: 1440, label: "🖥 桌面 1440" },
  { v: 768, label: "📱 平板 768" },
  { v: 390, label: "📱 手机 390" },
];

export default function DesignPanel({
  onToast,
}: {
  onToast: (m: string, e?: boolean) => void;
}) {
  const [path, setPath] = useState("");
  const [width, setWidth] = useState(1440);
  const [nonce, setNonce] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [shot, setShot] = useState<DesignShot | null>(null);
  const [check, setCheck] = useState<DesignCheck | null>(null);
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");

  const has = !!path.trim();

  function load() {
    if (!has) return;
    setErr("");
    setCheck(null);
    setShot(null);
    setLoaded(true);
    setNonce((k) => k + 1); // 换 key 强制刷新 iframe
  }

  async function takeShot() {
    if (!has) return;
    setBusy("shot");
    setErr("");
    try {
      const d = await postJSON<DesignShot>("/api/design/shot", { path, width });
      setShot(d);
      onToast(`截图完成 ${d.width}px`);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m);
      onToast(m, true);
    } finally {
      setBusy("");
    }
  }

  async function runCheck() {
    if (!has) return;
    setBusy("check");
    setErr("");
    try {
      const d = await postJSON<DesignCheck>("/api/design/check", { path, width });
      setCheck(d);
      onToast(d.issues.length ? `发现 ${d.issues.length} 个问题` : "没发现问题");
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m);
      onToast(m, true);
    } finally {
      setBusy("");
    }
  }

  return (
    <>
      <div className="col-head">
        设计
        <span className="spacer" />
        <span className="st-off">无头浏览器检测布局</span>
      </div>

      <div className="design-bar">
        <input
          value={path}
          placeholder="HTML 文件路径，如 ~/Projects/sandbox/landing.html"
          onChange={(e) => setPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") load();
          }}
        />
        <select value={width} onChange={(e) => setWidth(Number(e.target.value))}>
          {WIDTHS.map((w) => (
            <option key={w.v} value={w.v}>{w.label}</option>
          ))}
        </select>
        <button onClick={load} disabled={!has}>加载</button>
        <button onClick={takeShot} disabled={!has || busy === "shot"}>
          {busy === "shot" ? "截图中…" : "截图"}
        </button>
        <button className="primary" onClick={runCheck} disabled={!has || busy === "check"}>
          {busy === "check" ? "检测中…" : "检测"}
        </button>
      </div>

      <div className="design-main">
        <div className="design-preview">
          {!loaded && <div className="empty">填路径后点「加载」</div>}
          {loaded && (
            <iframe
              key={nonce}
              title="design-preview"
              src={`/api/preview?path=${encodeURIComponent(path)}`}
              style={{ width, height: "100%", minHeight: 400, border: 0, background: "#fff", flex: "none" }}
            />
          )}
          {shot && (
            <div className="shot-wrap">
              <div className="shot-title">截图 · {shot.width}px</div>
              <img src={shot.url} alt="截图" />
            </div>
          )}
        </div>

        <div className="design-report">
          {err && <div className="rag-err">{err}</div>}
          {!err && !check && <div className="empty">加载页面后点「检测」</div>}
          {check && (
            <>
              <div className="report-head">
                <b>{check.title || "（无标题）"}</b>
                <span>{check.width}px · {check.issues.length} 个问题</span>
              </div>
              {check.issues.length === 0 && <div className="ok-box">没发现问题 ✅</div>}
              {check.issues.map((it, i) => (
                <div key={i} className={`issue ${it.level}`}>
                  <div className="issue-type">{it.type}</div>
                  <div className="issue-detail">{it.detail}</div>
                  {it.sel && <div className="issue-sel">{it.sel}</div>}
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </>
  );
}
