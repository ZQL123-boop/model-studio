import { useState } from "react";
import { getToken, setToken } from "../auth";

/**
 * 远程访问缺 token 时的设置层。
 *
 * 为什么不做成「自动带上上次的 token」就完事：token 错误时后端只回 401，
 * 前端分不清「没填」和「填错了」，所以让用户看到输入框并自己确认。
 */
export default function TokenSetup({ onDone }: { onDone: () => void }) {
  const [val, setVal] = useState(getToken());
  const [err, setErr] = useState("");

  function submit() {
    const t = val.trim();
    if (!t) {
      setErr("请粘贴 token");
      return;
    }
    if (t.length < 16) {
      setErr("这串太短了，token 一般 24 位以上");
      return;
    }
    setToken(t);
    onDone();
  }

  return (
    <div className="gate">
      <div className="gate-card">
        <h2>需要访问 token</h2>
        <p>
          远程访问要走 API 鉴权。token 在这台 Mac 上：
          <br />
          <code>grep MODEL_STUDIO_TOKEN ~/Projects/model-studio/.env</code>
        </p>
        <input
          type="password"
          autoFocus
          value={val}
          placeholder="粘贴 MODEL_STUDIO_TOKEN"
          onChange={(e) => {
            setVal(e.target.value);
            setErr("");
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
          }}
        />
        {err && <div className="gate-err">{err}</div>}
        <button className="primary" onClick={submit}>连接</button>
        <div className="drawer-hint">
          也可以直接在地址后面加 <code>?token=xxx</code>，读一次就记住。
        </div>
      </div>
    </div>
  );
}
