import { useEffect, useRef, useState } from "react";
import { getJSON } from "../api";

export default function LogView() {
  const [text, setText] = useState("（未启动）");
  const boxRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const d = await getJSON<{ text: string }>("/api/logs?lines=200");
        if (alive) setText(d.text || "（空）");
      } catch {
        /* 后端重启时静默，不刷屏 */
      }
    };
    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  // 每次日志更新贴到底部
  useEffect(() => {
    const el = boxRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        minHeight: 0,
        borderTop: "1px solid var(--border)",
      }}
    >
      <div className="col-head">模型日志</div>
      <pre className="logbox" ref={boxRef}>{text}</pre>
    </div>
  );
}
