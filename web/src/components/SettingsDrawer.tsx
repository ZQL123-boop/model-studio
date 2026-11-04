import { useEffect, useState } from "react";
import { getJSON, postJSON } from "../api";
import type { GenSettings } from "../types";
import AccessSection from "./AccessSection";
import PasswordModal, { type PwAsk } from "./PasswordModal";

// 和旧版 static/app.html 的 THEME_COLORS 保持一致，避免两边颜色不一样
const THEME_COLORS = [
  "#58a6ff", "#3fb950", "#bc8cff", "#f0883e", "#ff7b72", "#39c5cf",
  "#d29922", "#db61a2", "#768390", "#e6edf3", "#f85149", "#0d1117",
];

interface Props {
  open: boolean;
  settings: GenSettings;
  running: boolean;
  onChange: (s: GenSettings) => void;
  onClose: () => void;
  onToast: (msg: string, err?: boolean) => void;
}

export default function SettingsDrawer({
  open, settings, running, onChange, onClose, onToast,
}: Props) {
  /* ------------------------------------------------ 密码弹窗（promise 风格） */
  const [ask, setAsk] = useState<PwAsk | null>(null);
  const askPassword = (title: string) =>
    new Promise<string>((resolve) => setAsk({ title, resolve }));
  const finishAsk = (v: string) => {
    ask?.resolve(v);
    setAsk(null);
  };

  /* ------------------------------------------------ 主题色 */
  const [accent, setAccent] = useState(
    () => localStorage.getItem("ms_accent") || "#58a6ff",
  );
  useEffect(() => {
    document.documentElement.style.setProperty("--accent", accent);
    try {
      localStorage.setItem("ms_accent", accent);
    } catch {
      /* 隐私模式 */
    }
  }, [accent]);

  /* ------------------------------------------------ 写保护 */
  const [wp, setWp] = useState<boolean | null>(null);
  useEffect(() => {
    if (!open) return;
    getJSON<{ on: boolean }>("/api/tools/write-protect")
      .then((d) => setWp(d.on))
      .catch(() => {});
  }, [open]);

  async function toggleWp() {
    try {
      const d = await postJSON<{ on: boolean }>("/api/tools/write-protect", { on: !wp });
      setWp(d.on);
      onToast(d.on ? "已开启写保护：不覆盖已存在文件" : "已关闭写保护");
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    }
  }

  const set = <K extends keyof GenSettings>(k: K, v: GenSettings[K]) =>
    onChange({ ...settings, [k]: v });

  return (
    <>
      {open && <div className="drawer-mask" onClick={onClose} />}
      <aside className={`drawer${open ? " open" : ""}`}>
        <div className="drawer-head">
          <h2>设置</h2>
          <button onClick={onClose}>×</button>
        </div>

        <div className="drawer-sec">生成参数</div>

        <Param
          label="temperature" value={settings.temperature}
          min={0} max={2} step={0.05}
          onChange={(v) => set("temperature", v)}
        />
        <Param
          label="top_p" value={settings.top_p}
          min={0.1} max={1} step={0.05}
          onChange={(v) => set("top_p", v)}
        />
        <Param
          label="max_tokens" value={settings.max_tokens}
          min={32} max={4096} step={32}
          onChange={(v) => set("max_tokens", v)}
        />

        <label className="drawer-check">
          <input
            type="checkbox"
            checked={settings.thinking}
            onChange={(e) => set("thinking", e.target.checked)}
          />
          <span>开启 thinking（需重启模型，会慢 10 倍）</span>
        </label>
        {running && <div className="drawer-hint">thinking 改动要重启模型才生效</div>}

        <AccessSection askPassword={askPassword} onToast={onToast} />

        <div className="drawer-sec">主题色</div>
        <div className="swatch-row">
          {THEME_COLORS.map((c) => (
            <span
              key={c}
              className={`swatch${accent === c ? " on" : ""}`}
              style={{ background: c }}
              title={c}
              onClick={() => setAccent(c)}
            />
          ))}
        </div>

        <div className="drawer-sec">工具</div>
        <div className="set-item">
          <div className="set-main">
            <div className="set-title">写保护</div>
            <div className="set-sub">不覆盖已存在的文件</div>
          </div>
          <label className="switch">
            <input type="checkbox" checked={!!wp} onChange={toggleWp} />
            <i />
          </label>
        </div>

        <div className="drawer-sec">旧版</div>
        <a
          className="drawer-link"
          href="/static/app.html"
          target="_blank"
          rel="noreferrer"
        >
          旧版完整界面（知识库 / 记忆 / 设计）↗
        </a>
        <div className="drawer-hint">这三块还没迁到新版，先用旧页面。</div>
      </aside>

      <PasswordModal ask={ask} onDone={finishAsk} />
    </>
  );
}

function Param({
  label, value, min, max, step, onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <div className="drawer-field">
      <label>
        {label} <b>{value}</b>
      </label>
      <input
        type="range"
        min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </div>
  );
}
