/**
 * 访问 token：远程访问要带 `Authorization: Bearer <token>`，本机不需要。
 *
 * 来源优先级：URL 参数 `?token=xxx` → sessionStorage。
 * URL 里的读完立刻从地址栏抹掉，避免留在浏览历史 / 截图里。
 */
const KEY = "ms_token";

/** 启动时调一次：把 URL 里的 token 落到 sessionStorage 并清理地址栏。 */
export function initToken(): void {
  try {
    const fromUrl = new URLSearchParams(location.search).get("token");
    if (fromUrl) {
      sessionStorage.setItem(KEY, fromUrl);
      history.replaceState(null, "", location.pathname);
    }
  } catch {
    /* 隐私模式 / 非浏览器环境 */
  }
}

export function getToken(): string {
  try {
    return sessionStorage.getItem(KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(t: string): void {
  try {
    if (t) sessionStorage.setItem(KEY, t);
    else sessionStorage.removeItem(KEY);
  } catch {
    /* 忽略 */
  }
}

/** 是否本机访问（本机后端免 token）。 */
export function isLocalHost(): boolean {
  return ["127.0.0.1", "localhost", "::1"].includes(location.hostname);
}

/** 本机安装的 token（节选）：填错时给个提示，不暴露完整值。 */
export function tokenHint(): string {
  const t = getToken();
  return t ? `${t.slice(0, 6)}…（${t.length} 位）` : "";
}
