import { useState } from "react";
import { postJSON } from "../api";

/** 门禁登录层：界面设了访问密码时，先过这一关再渲染主界面。 */
export default function GateLogin({ onPass }: { onPass: () => void }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (!pw || busy) return;
    setBusy(true);
    setErr("");
    try {
      await postJSON("/api/gate/verify", { password: pw });
      onPass();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <div className="gate-card">
        <h2>Model Studio</h2>
        <p>这个界面设置了访问密码</p>
        <input
          type="password"
          autoFocus
          value={pw}
          placeholder="输入访问密码"
          onChange={(e) => setPw(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        {err && <div className="gate-err">{err}</div>}
        <button className="primary" disabled={busy || !pw} onClick={submit}>
          {busy ? "验证中…" : "进入"}
        </button>
      </div>
    </div>
  );
}
