import type { StreamFrame } from "./types";

const JSON_HEADERS = { "Content-Type": "application/json" };

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return (await r.json()) as T;
}

export async function postJSON<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, "POST", body);
}

async function request<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? undefined : JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
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

export const putJSON = <T>(path: string, body?: unknown) =>
  request<T>(path, "PUT", body);

export const delJSON = <T>(path: string) => request<T>(path, "DELETE");

/** 读后端 SSE，逐帧 yield。后端每帧是 `data: {...}\n\n`，结束发 `data: [DONE]`。 */
export async function* streamSSE(
  path: string,
  body: unknown,
): AsyncGenerator<StreamFrame> {
  const r = await fetch(path, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  if (!r.ok || !r.body) {
    const text = await r.text().catch(() => "");
    throw new Error(text || `${r.status} ${r.statusText}`);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
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
}
