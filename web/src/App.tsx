import { useCallback, useEffect, useRef, useState } from "react";
import { getJSON, postJSON } from "./api";
import type {
  CleanResult,
  GateStatus,
  GenSettings,
  LocalModel,
  ModelStatus,
  ModelsResp,
  Provider,
} from "./types";
import ModelList from "./components/ModelList";
import ChatPanel from "./components/ChatPanel";
import StatusPanel from "./components/StatusPanel";
import LogView from "./components/LogView";
import SettingsDrawer from "./components/SettingsDrawer";
import GateLogin from "./components/GateLogin";
import RagPanel from "./components/RagPanel";
import MemoryPanel from "./components/MemoryPanel";
import DesignPanel from "./components/DesignPanel";

const TABS = [
  ["chat", "对话"],
  ["rag", "知识库"],
  ["memory", "记忆"],
  ["design", "设计"],
] as const;

type TabKey = (typeof TABS)[number][0];

interface Toast {
  id: number;
  msg: string;
  err?: boolean;
}

const DEFAULT_SETTINGS: GenSettings = {
  temperature: 0.7,
  top_p: 0.9,
  max_tokens: 512,
  thinking: false,
};

const LS_KEY = "ms_gen_settings";

function loadSettings(): GenSettings {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (raw) {
      return { ...DEFAULT_SETTINGS, ...(JSON.parse(raw) as Partial<GenSettings>) };
    }
  } catch {
    /* 坏数据当没有 */
  }
  return DEFAULT_SETTINGS;
}

export default function App() {
  const [status, setStatus] = useState<ModelStatus | null>(null);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [selected, setSelected] = useState<LocalModel | null>(null);
  const [provider, setProvider] = useState("auto");
  const [useAgent, setUseAgent] = useState(true);
  const [busy, setBusy] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [settings, setSettings] = useState<GenSettings>(loadSettings);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [tab, setTab] = useState<TabKey>("chat");
  // null = 还在查；true = 放行；false = 要输密码
  const [gateOk, setGateOk] = useState<boolean | null>(null);

  // loadModels 里要判断"是否已选"，用 ref 避免把它塞进依赖导致重复拉取
  const selectedRef = useRef<LocalModel | null>(null);
  selectedRef.current = selected;

  // 参数改动即时落盘，刷新页面还在
  useEffect(() => {
    try {
      localStorage.setItem(LS_KEY, JSON.stringify(settings));
    } catch {
      /* 隐私模式写不进去，无所谓 */
    }
  }, [settings]);

  const toast = useCallback((msg: string, err = false) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, err }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);

  const loadModels = useCallback(async () => {
    try {
      const d = await getJSON<ModelsResp>("/api/models");
      setModels(d.local || []);
      if (d.api) setProviders(d.api);
      if (!selectedRef.current) setSelected(d.local?.[0] ?? null);
    } catch {
      toast("加载模型列表失败", true);
    }
  }, [toast]);

  useEffect(() => {
    loadModels();
  }, [loadModels]);

  // 状态轮询：2 秒一次，后端重启期间静默
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await getJSON<ModelStatus>("/api/status");
        if (!alive) return;
        setStatus(s);
        if (s.providers) setProviders(s.providers);
      } catch {
        /* 忽略 */
      }
    };
    tick();
    const id = window.setInterval(tick, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  // 门禁：设了访问密码就先过登录层（会话内有效，关标签页重来）
  useEffect(() => {
    (async () => {
      try {
        const g = await getJSON<GateStatus>("/api/gate/status");
        if (!g.has_password) {
          setGateOk(true);
          return;
        }
        let passed = false;
        try {
          passed = sessionStorage.getItem("ms_gate_ok") === "1";
        } catch {
          /* 隐私模式 */
        }
        setGateOk(passed);
      } catch {
        setGateOk(true); // 后端刚重启查不到，本机场景放行
      }
    })();
  }, []);

  const start = async () => {
    if (!selected) {
      toast("先选一个模型", true);
      return;
    }
    setBusy(true);
    try {
      await postJSON("/api/start", {
        model_id: selected.id,
        port: 8080,
        max_tokens: 2048,
        thinking: settings.thinking,
      });
      toast("已启动，等待就绪…");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await postJSON("/api/stop", {});
      toast("已停止");
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  };

  const clean = async () => {
    setBusy(true);
    try {
      const d = await postJSON<CleanResult>("/api/mem/clean");
      toast(
        d.freed_gb >= 0.01
          ? `已释放 ${d.freed_gb.toFixed(2)} GB`
          : `可用内存 ${d.after_gb.toFixed(1)} GB，没有可释放的残留`,
      );
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setBusy(false);
    }
  };

  if (gateOk === null) {
    return (
      <div className="gate">
        <div className="gate-card">
          <h2>Model Studio</h2>
          <p>检查访问权限…</p>
        </div>
      </div>
    );
  }
  if (!gateOk) {
    return (
      <GateLogin
        onPass={() => {
          try {
            sessionStorage.setItem("ms_gate_ok", "1");
          } catch {
            /* 隐私模式 */
          }
          setGateOk(true);
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="topbar">
        <h1>Model Studio</h1>
        <span className="chip">
          <span className={`dot ${status?.state ?? ""}`} />
          {status?.state === "ready" ? (
            <>本地 · <b>{status.model}</b> :{status.port}</>
          ) : status?.state === "starting" ? (
            "加载中…"
          ) : status?.state === "error" ? (
            "启动失败"
          ) : (
            "未启动"
          )}
        </span>
        <span className="chip">
          内存{" "}
          <b>
            {status
              ? `${status.sys.used_gb.toFixed(1)} / ${status.sys.total_gb.toFixed(1)} GB`
              : "—"}
          </b>
        </span>
        <span className="chip">
          CPU <b>{status ? `${status.sys.cpu_pct.toFixed(0)}%` : "—"}</b>
        </span>
        <span className="spacer" />
        <button onClick={loadModels}>刷新模型</button>
        <button onClick={() => setSettingsOpen(true)}>⚙️ 设置</button>
      </header>

      <div className="main">
        <aside className="col">
          <ModelList
            models={models}
            status={status}
            selected={selected}
            busy={busy}
            onSelect={setSelected}
            onStart={start}
            onStop={stop}
            onClean={clean}
          />
        </aside>

        <section className="col">
          <div className="tabs">
            {TABS.map(([k, label]) => (
              <div
                key={k}
                className={`tab${tab === k ? " on" : ""}`}
                onClick={() => setTab(k)}
              >
                {label}
              </div>
            ))}
          </div>

          {tab === "chat" && (
            <ChatPanel
              ready={status?.state === "ready"}
              providers={providers}
              provider={provider}
              onProviderChange={setProvider}
              useAgent={useAgent}
              onUseAgentChange={setUseAgent}
              settings={settings}
              onToast={toast}
            />
          )}
          {tab === "rag" && <RagPanel onToast={toast} />}
          {tab === "memory" && <MemoryPanel onToast={toast} />}
          {tab === "design" && <DesignPanel onToast={toast} />}
        </section>

        <aside className="col right">
          <StatusPanel status={status} />
          <LogView />
        </aside>
      </div>

      <SettingsDrawer
        open={settingsOpen}
        settings={settings}
        running={status?.state === "ready" || status?.state === "starting"}
        onChange={setSettings}
        onClose={() => setSettingsOpen(false)}
        onToast={toast}
      />

      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast${t.err ? " err" : ""}`}>
            {t.msg}
          </div>
        ))}
      </div>
    </div>
  );
}
