import { useCallback, useEffect, useState } from "react";
import { getJSON, postJSON } from "../api";
import type { AccessToken, GateStatus } from "../types";

interface Props {
  askPassword: (title: string) => Promise<string>;
  onToast: (msg: string, err?: boolean) => void;
}

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

/**
 * 访问控制：界面门禁 + 访问 token。
 *
 * 模型锁已移除——本机免密、远程的控制权操作本来就被 `_require_local` 挡住，
 * 锁密码只剩「远程管 token」一个用途，而那个也被 `_require_local` 拦了，
 * 等于死功能。
 */
export default function AccessSection({ askPassword, onToast }: Props) {
  const [gate, setGate] = useState<GateStatus | null>(null);

  const [tokens, setTokens] = useState<AccessToken[] | null>(null);
  const [masterPreview, setMasterPreview] = useState("");
  const [newName, setNewName] = useState("");
  const [newTok, setNewTok] = useState("");

  const refresh = useCallback(async () => {
    try {
      setGate(await getJSON<GateStatus>("/api/gate/status"));
    } catch {
      /* 后端重启中，静默 */
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  /* ------------------------------------------------------------ 门禁 */
  async function setGatePw() {
    const needOld = !!gate?.has_password;
    const old = needOld ? await askPassword("输入旧的门禁密码") : "";
    if (needOld && !old) return;
    const pw = await askPassword("设置新门禁密码（至少 4 位）");
    if (!pw) return;
    try {
      await postJSON("/api/gate/password", {
        password: pw,
        old_password: needOld ? old : undefined,
      });
      onToast("门禁密码已设置");
      refresh();
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  async function clearGate() {
    const pw = await askPassword("输入门禁密码以关闭");
    if (!pw) return;
    try {
      await postJSON("/api/gate/verify", { password: pw });
      await postJSON("/api/gate/clear", {});
      onToast("门禁已关闭");
      refresh();
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  /* ------------------------------------------------------------ Token */
  async function openTokens() {
    try {
      const d = await postJSON<{ master_preview: string; tokens: AccessToken[] }>(
        "/api/tokens",
        {},
      );
      setTokens(d.tokens);
      setMasterPreview(d.master_preview);
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  async function addToken() {
    const name = newName.trim();
    if (!name) return;
    try {
      const d = await postJSON<{ msg: string; token: string }>("/api/tokens/add", { name });
      setNewName("");
      setNewTok(d.token);
      onToast("已生成，复制给要分享的人");
      await openTokens();
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  async function toggleToken(t: AccessToken) {
    try {
      await postJSON("/api/tokens/toggle", {
        name_or_token: t.name,
        enabled: !t.enabled,
      });
      await openTokens();
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  async function removeToken(t: AccessToken) {
    if (!window.confirm(`删除 token「${t.name}」？`)) return;
    try {
      await postJSON("/api/tokens/remove", { name_or_token: t.name });
      onToast("已删除");
      await openTokens();
    } catch (e) {
      onToast(errMsg(e), true);
    }
  }

  return (
    <>
      <div className="drawer-sec">访问控制</div>

      <div className="set-item">
        <div className="set-main" onClick={setGatePw}>
          <div className="set-title">🔑 界面门禁</div>
          <div className="set-sub">远程打开界面要密码</div>
        </div>
        <div className="set-side">
          <span className={gate?.has_password ? "st-on" : "st-off"}>
            {gate?.has_password ? "已设密码" : "未设置"}
          </span>
          {gate?.has_password && (
            <button className="mini danger" onClick={clearGate}>关闭</button>
          )}
        </div>
      </div>

      <div className="set-item column">
        <div className="set-main" onClick={openTokens}>
          <div className="set-title">👤 访问 token</div>
          <div className="set-sub">远程调 API 要带，给不同人发不同 token</div>
        </div>
        <span className="set-arrow">›</span>

        {tokens !== null && (
          <div className="token-box">
            <div className="token-master">
              主 token：{masterPreview || "无"}（在 .env，永远有效）
            </div>
            {tokens.length === 0 && <div className="token-empty">还没有附加 token</div>}
            {tokens.map((t) => (
              <div key={t.name} className="token-row">
                <div className="token-info">
                  <div className={`token-name${t.enabled ? "" : " off"}`}>{t.name}</div>
                  <div className="token-preview">
                    {t.preview}
                    {t.enabled ? "" : "（已禁用）"}
                  </div>
                </div>
                <button className="mini" onClick={() => toggleToken(t)}>
                  {t.enabled ? "禁用" : "启用"}
                </button>
                <button className="mini danger" onClick={() => removeToken(t)}>
                  删除
                </button>
              </div>
            ))}
            <div className="token-add">
              <input
                value={newName}
                placeholder="备注，如：给小王"
                onChange={(e) => setNewName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") addToken();
                }}
              />
              <button onClick={addToken} disabled={!newName.trim()}>添加</button>
            </div>
            {newTok && (
              <div className="token-new">
                新 token：<code>{newTok}</code>
                <div className="drawer-hint">只显示这一次，先复制再关。</div>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
