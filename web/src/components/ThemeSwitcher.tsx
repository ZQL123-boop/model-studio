import { useEffect, useRef, useState } from "react";

// 和旧版 static/app.html 的 THEME_COLORS 保持一致
export const THEME_COLORS = [
  "#58a6ff", "#3fb950", "#bc8cff", "#f0883e", "#ff7b72", "#39c5cf",
  "#d29922", "#db61a2", "#768390", "#e6edf3", "#f85149", "#0d1117",
];

/** 顶栏主题色切换：一个圆点按钮 + 点开后的色板。 */
export default function ThemeSwitcher({
  accent,
  onChange,
}: {
  accent: string;
  onChange: (c: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // 点外面关掉
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div className="theme-switch" ref={ref}>
      <button
        className="theme-btn"
        onClick={() => setOpen((v) => !v)}
        title="主题色"
      >
        <span className="theme-dot" style={{ background: accent }} />
        主题
      </button>
      {open && (
        <div className="theme-pop">
          {THEME_COLORS.map((c) => (
            <span
              key={c}
              className={`swatch${accent === c ? " on" : ""}`}
              style={{ background: c }}
              title={c}
              onClick={() => {
                onChange(c);
                setOpen(false);
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}
