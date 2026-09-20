/**
 * Client-side diagnostics ring buffer and the THREE separated health states
 * (docs/09 §三种健康 + §UI Inspector).
 *
 * - at most 200 events are kept, in memory only, for the current session;
 * - a request summary records method, route template (never the full URL with
 *   parameters), status, duration and trace id — never a body;
 * - FrontendHealth / BackendHealth / ResearchQuality are computed separately and
 *   are never merged into one "all green" indicator.
 */

export interface ClientEvent {
  at: string;
  kind: 'request' | 'schema' | 'render' | 'pdf' | 'capability';
  detail: string;
  method?: string;
  routeTemplate?: string;
  status?: number;
  durationMs?: number;
  traceId?: string | null;
  errorCode?: string | null;
}

export const MAX_CLIENT_EVENTS = 200;

const events: ClientEvent[] = [];

export function recordEvent(event: Omit<ClientEvent, 'at'> & { at?: string }): void {
  events.push({ at: event.at ?? new Date().toISOString(), ...event });
  // Keep the most recent events: the buffer is bounded by design.
  while (events.length > MAX_CLIENT_EVENTS) events.shift();
}

export function clientEvents(): ClientEvent[] {
  return [...events];
}

export function clearClientEvents(): void {
  events.length = 0;
}

/** `/v1/ui/papers/pap_123/workspace` → `/v1/ui/papers/{id}/workspace` */
export function routeTemplate(path: string): string {
  return path
    .split('?')[0]!
    .split('/')
    .map((segment) =>
      /^(pap|pver|ast|ev|clm|ver|ent|rel|tag|col|job|tsk|run|call|trc|prv|mdl|prm|uin|urd|uss|uib|uit|uop)_/.test(
        segment,
      )
        ? '{id}'
        : segment,
    )
    .join('/');
}

export type HealthState = 'OK' | 'DEGRADED' | 'DOWN' | 'UNKNOWN';

export interface HealthReport {
  state: HealthState;
  reasons: string[];
  observedAt: string | null;
}

export interface FrontendHealthInput {
  lastRequestStatus?: number | null;
  lastRequestAt?: string | null;
  schemaIssueCount: number;
  pdfEngineError?: string | null;
  capabilitiesLoaded: boolean;
}

/** FrontendHealth: rendering, API parsing, the PDF worker, caches. */
export function frontendHealth(input: FrontendHealthInput): HealthReport {
  const reasons: string[] = [];
  let state: HealthState = 'OK';
  if (!input.capabilitiesLoaded) {
    state = 'UNKNOWN';
    reasons.push('capabilities 尚未读取');
  }
  if (input.schemaIssueCount > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.schemaIssueCount} 个契约校验问题`);
  }
  if (input.lastRequestStatus && input.lastRequestStatus >= 500) {
    state = 'DOWN';
    reasons.push(`最后一次请求返回 ${input.lastRequestStatus}`);
  }
  if (input.pdfEngineError) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`PDF worker: ${input.pdfEngineError}`);
  }
  return { state, reasons, observedAt: input.lastRequestAt ?? null };
}

export interface BackendHealthInput {
  providerUnknownCount: number;
  workerUnobserved: boolean;
  queueFailed: number;
  diskUnmeasurable: boolean;
}

/** BackendHealth: probes, DB/Redis/worker, storage, providers, execution. */
export function backendHealth(input: BackendHealthInput): HealthReport {
  const reasons: string[] = [];
  let state: HealthState = 'OK';
  if (input.workerUnobserved) {
    state = 'UNKNOWN';
    reasons.push('没有 worker 心跳：在线 worker 数未观测，不能由运行中任务推断');
  }
  if (input.providerUnknownCount > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.providerUnknownCount} 个 Provider 没有真实采样`);
  }
  if (input.diskUnmeasurable) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push('磁盘不可测量（不是 0 字节）');
  }
  if (input.queueFailed > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.queueFailed} 个任务失败`);
  }
  return { state, reasons, observedAt: null };
}

export interface ResearchQualityInput {
  disputed: number;
  unsupported: number;
  notVerified: number;
  staleVersions: number;
}

/** ResearchQuality: evidence quality, support states, disputes, version validity. */
export function researchQualityHealth(input: ResearchQualityInput): HealthReport {
  const reasons: string[] = [];
  let state: HealthState = 'OK';
  if (input.notVerified > 0) {
    state = 'UNKNOWN';
    reasons.push(`${input.notVerified} 条结论尚未核查`);
  }
  if (input.disputed > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.disputed} 条结论存在争议`);
  }
  if (input.unsupported > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.unsupported} 条结论证据不足`);
  }
  if (input.staleVersions > 0) {
    state = state === 'OK' ? 'DEGRADED' : state;
    reasons.push(`${input.staleVersions} 个版本已过期`);
  }
  return { state, reasons, observedAt: null };
}

export const HEALTH_LABELS: Record<HealthState, string> = {
  OK: '正常',
  DEGRADED: '部分降级',
  DOWN: '不可用',
  UNKNOWN: '未观测',
};
