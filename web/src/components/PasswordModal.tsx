import { useEffect, useRef, useState } from "react";

export interface PwAsk {
  title: string;
  resolve: (v: string) => void;
}

interface Props {
  ask: PwAsk | null;
  onDone: (v: string) => void;
}

/**
 * 密码输入弹窗（替代 window.prompt，移动端也能用）。
 * 由父组件用 promise 风格驱动：askPassword() 返回 Promise<string>，取消返回 ""。
 */
export default function PasswordModal({ ask, onDone }: Props) {
  const [val, setVal] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setVal("");
    if (ask) {
      // 等一帧再聚焦，否则 iOS 上软键盘不弹
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [ask]);

  if (!ask) return null;

  return (
    <div className="modal-mask" onClick={() => onDone("")}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h3>{ask.title}</h3>
        <input
          ref={inputRef}
          type="password"
          value={val}
          placeholder="密码"
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onDone(val);
            if (e.key === "Escape") onDone("");
          }}
        />
        <div className="modal-actions">
          <button onClick={() => onDone("")}>取消</button>
          <button className="primary" onClick={() => onDone(val)}>确定</button>
        </div>
      </div>
    </div>
  );
}
