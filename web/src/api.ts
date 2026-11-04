import { getToken } from "./auth";
import type { StreamFrame } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

/** 远程访问缺 token（或 token 错）时抛这个，上层据此显示 token 输入层。 */
export class NeedTokenError extends Error {
  constructor() {
    super("需要访问 token");
    this.name = "NeedTokenError";
  }
}

/** 本机后端放行，远程要 Bearer token；有就带上，没有也照发（后端会 401）。 */
function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) };
  const t = getToken();
  if (t) h.Authorization = `Bearer ${t}`;
  return h;
}

/**
 * 401 有两种来源，必须分开：
 *   - 中间件：「需要 Authorization: Bearer」→ 缺 token，弹 token 层
 *   - 接口自己：密码错（gate/verify、lock/verify、tokens 列表）→ 提示密码错
 *
 * 光看状态码分不出来（都是 401），所以看响应体里的 detail。
 * 之前只排除了 /api/gate，导致远程输错锁密码时弹出「需要访问 token」，
 * 用户完全不知道发生了什么。
 */
function handle401(path: string, text: string): never {
  if (path.startsWith("/api/gate") || text.includes("密码错误")) {
    throw new Error("密码错误");
  }
  throw new NeedTokenError();
}

async function request<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: authHeaders(body === undefined ? undefined : JSON_HEADERS),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();

  if (r.status === 401) handle401(path, text);

  if (!r.ok) {
    let msg = text;
    try {
      const d = JSON.parse(text);
      msg = d.detail || d.error || text;
    } catch {
      /* 非 JSON 错误体，原样抛出 */
    }
    throw new Error(msg || `${r.status} ${r.statusText}`);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

export const getJSON = <T>(path: string) => request<T>(path, "GET");

export async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, "POST", body);
}

export const putJSON = <T>(path: string, body?: unknown) =>
  request<T>(path, "PUT", body);

export const delJSON = <T>(path: string) => request<T>(path, "DELETE");

/** 读后端 SSE，逐帧 yield。后端每帧是 `data: {...}\n\n`，结束发 `data: [DONE]`。
 *  signal 可选：调用方 abort 时必须传给 fetch，否则只是 UI 停了，连接还会跑完。 */
export async function* streamSSE(
  path: string,
  body: unknown,
  signal?: AbortSignal,
): AsyncGenerator<StreamFrame> {
  const r = await fetch(path, {
    method: "POST",
    headers: authHeaders(JSON_HEADERS),
    body: JSON.stringify(body),
    signal,
  });
  if (r.status === 401) {
    const text = await r.text().catch(() => "");
    handle401(path, text);
  }
  if (!r.ok || !r.body) {
    const text = await r.text().catch(() => "");
    throw new Error(text || `${r.status} ${r.statusText}`);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.trim();
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (raw === "[DONE]") return;
        try {
          yield JSON.parse(raw) as StreamFrame;
        } catch {
          /* 半包，丢 */
        }
      }
    }
  } finally {
    // 只在用户中断时 cancel。正常结束也 cancel 的话，浏览器会把这次请求记成 canceled
    if (signal?.aborted) reader.cancel().catch(() => {});
  }
}
