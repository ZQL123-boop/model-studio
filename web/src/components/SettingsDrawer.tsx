import { useState } from "react";
import type { GenSettings } from "../types";
import AccessSection from "./AccessSection";
import PasswordModal, { type PwAsk } from "./PasswordModal";

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

        <div className="drawer-sec">旧版</div>
        <a
          className="drawer-link"
          href="/static/app.html"
          target="_blank"
          rel="noreferrer"
        >
          旧版单页（留作备用）↗
        </a>
        <div className="drawer-hint">
          主题色和写保护已经移到主界面顶栏。
        </div>
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
