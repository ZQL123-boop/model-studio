export type RunState = "stopped" | "starting" | "ready" | "error";

export interface SysInfo {
  total_gb: number;
  used_gb: number;
  free_pct: number;
  cpu_pct: number;
  cores: number;
}

export interface Provider {
  id: string;
  name: string;
  has_key: boolean;
  key_source: string;
}

export interface ModelStatus {
  state: RunState;
  model: string | null;
  model_id: string | null;
  pid: number | null;
  port: number;
  max_tokens?: number;
  uptime_s: number;
  error: string;
  rss_gb: number;
  sys: SysInfo;
  providers?: Provider[];
}

export interface LocalModel {
  id: string;
  name: string;
  path: string;
  kind: string;
  size_gb: number;
  ready: boolean;
  note: string;
}

export interface ModelsResp {
  local: LocalModel[];
  api?: Provider[];
}

export interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  steps?: string[];
  failed?: boolean;
  ttft?: number;      // 首 token 延迟（秒）
  elapsed?: number;   // 总耗时（秒）
  downloads?: { name: string; url: string }[];   // agent 生成的文件，浏览器下载到本机
}

export interface StreamFrame {
  t?: string;
  r?: string;
  done?: boolean;
  error?: string;
  answer?: string;
  step_start?: { tool: string; args?: unknown };
  step?: { tool: string; args?: unknown; preview?: string; ms?: number };
  ttft_s?: number;
  elapsed_s?: number;
  chunks?: number;
  chunk_per_s?: number;
  changed?: string[];   // agent done 帧：本次写过的路径
}

/** 一次工具调用的追踪记录（trace 面板用）。 */
export interface TraceItem {
  id: string;
  at: number;          // 开始时间（ms 时间戳）
  tool: string;
  args: Record<string, unknown>;
  preview: string;
  ms?: number;
  ok: boolean;
}

export interface CleanResult {  before_gb: number;
  after_gb: number;
  freed_gb: number;
  killed: { pid: number; rss_gb: number }[];
  state: RunState;
}

export interface GenSettings {
  temperature: number;
  top_p: number;
  max_tokens: number;
  thinking: boolean;
}

export interface GateStatus {
  has_password: boolean;
}

export interface AccessToken {
  name: string;
  token: string;
  enabled: boolean;
  preview: string;
}

/* ---------------------------------------------------------------- 知识库 */
export interface RagChunk {
  doc_id: string;
  header_path?: string;
  score?: number;
  body?: string;
}

export interface RagAnswer {
  question?: string;
  answer: string;
  citations?: string[];
  cite_index?: number[];
  chunks?: RagChunk[];
  timings?: { retrieve_ms?: number; rerank_ms?: number; generate_ms?: number };
  cos?: number;
  refused?: boolean;
  model?: string;
}

export interface RagHealth {
  state: string;
  detail?: string;
  elapsed_s?: number;
  has_reranker?: boolean;
  replay?: number;
}

export interface ReplayItem {
  qid: string;
  question: string;
  answerable: boolean;
  neg_type: string;
  refused: boolean;
  faithfulness: number;
  answer_relevancy: number;
  latency_ms: number;
  citations: number;
  chunks: number;
}

export interface ReplayOne {
  qid: string;
  question: string;
  answerable: boolean;
  neg_type: string;
  gold_answer: string;
  answer: string;
  contexts: string[];
  context_docs: string[];
  rerank_ms: number;
  latency_ms: number;
  refused: boolean;
  citations: string[];
  bad_citations: string[];
  no_citation: boolean;
  index_cite: boolean;
  faithfulness: number;
  answer_relevancy: number;
  gold_spans: {
    doc_id: string;
    start: number;
    end: number;
    primary: boolean;
    anchor: string;
  }[];
  chunks: RagChunk[];
}

/* ---------------------------------------------------------------- 记忆 */
export interface MemorySettings {
  enabled: boolean;
  sysinfo: boolean;
  depth: number;
  max_records: number;
}

export interface MemoryStats {
  settings: MemorySettings;
  count: number;
  size_kb: number;
  path: string;
}

export interface MemoryRecord {
  ts: string;
  model: string;
  question: string;
  answer: string;
  elapsed_s: number;
  ttft_s: number;
  completion_tokens?: number;
}

export interface MemoryHistory {
  count: number;
  records: MemoryRecord[];
}

export interface MemoryNotes {
  text: string;
  chars: number;
  max_chars: number;
  path: string;
}

/* ---------------------------------------------------------------- 设计 */
export interface DesignIssue {
  type: string;
  level: string;
  detail: string;
  sel: string;
}

export interface DesignCheck {
  width: number;
  title: string;
  issues: DesignIssue[];
}

export interface DesignShot {
  url: string;
  file: string;
  width: number;
}
