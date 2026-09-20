/**
 * The one HTTP client every feature uses (spec docs/06, docs/08).
 *
 * Rules encoded here:
 * - same-origin requests carry cookies; writes carry the CSRF token;
 * - every request sends X-Client-Request-ID and records X-Trace-ID;
 * - 401/403/409/412/422 are never retried automatically;
 * - network reads retry at most twice with 5/10/20/30s backoff (docs/08);
 * - no fixture fallback in LIVE mode: a failure is an error, never demo data.
 */

import { ApiError, isNonRetryable, normaliseError } from './errors';

export type Mode = 'LIVE' | 'TEST';

export interface ClientOptions {
  baseUrl?: string;
  mode: Mode;
  fetchImpl?: typeof fetch;
  /** Injected clock/sleep so tests do not wait real seconds. */
  sleepImpl?: (ms: number) => Promise<void>;
  maxReadRetries?: number;
  /**
   * Diagnostics sink (docs/09 §UI Inspector). It receives method, route
   * TEMPLATE, status, duration and trace id — never a body, a query string or a
   * credential.
   */
  onEvent?: (event: {
    method: string;
    path: string;
    status: number;
    durationMs: number;
    traceId: string | null;
    errorCode?: string | null;
  }) => void;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  body?: unknown;
  /** Opaque cursor; never a client-side offset. */
  signal?: AbortSignal;
  /** Idempotency key for side-effecting calls (docs/05 §Mutation协议). */
  idempotencyKey?: string;
  /** If-Match style concurrency guard (notes/preferences). */
  expectedRevision?: number;
  csrfToken?: string | null;
  contentType?: string;
}

export interface ClientResponse<T> {
  data: T;
  traceId: string;
  snapshotId: string | null;
  partial: boolean;
  warnings: Array<{ code: string; message: string }>;
}

const RETRY_DELAYS_MS = [5000, 10000] as const;

export interface UploadOptions {
  csrfToken?: string | null;
  signal?: AbortSignal;
}

let clientRequestCounter = 0;

function nextRequestId(): string {
  clientRequestCounter += 1;
  return `ui-${Date.now().toString(36)}-${clientRequestCounter}`;
}

export class ApiClient {
  private readonly options: ClientOptions;

  constructor(options: ClientOptions) {
    this.options = {
      baseUrl: '',
      maxReadRetries: RETRY_DELAYS_MS.length,
      ...options,
    };
  }

  get mode(): Mode {
    return this.options.mode;
  }

  async request<T>(path: string, options: RequestOptions = {}): Promise<ClientResponse<T>> {
    const method = options.method ?? 'GET';
    const isWrite = method !== 'GET';
    const attempts = isWrite ? 1 : (this.options.maxReadRetries ?? 0) + 1;
    let lastError: ApiError | null = null;

    for (let attempt = 0; attempt < attempts; attempt += 1) {
      if (attempt > 0) {
        const delay = RETRY_DELAYS_MS[Math.min(attempt - 1, RETRY_DELAYS_MS.length - 1)] ?? 10000;
        await this.sleep(delay);
      }
      try {
        return await this.attempt<T>(path, method, options);
      } catch (error) {
        if (!(error instanceof ApiError)) throw error;
        lastError = error;
        const retryable = error.retryable && !isNonRetryable(error.shape.http_status);
        if (!retryable || attempt === attempts - 1) throw error;
      }
    }
    throw lastError ?? new Error('unreachable');
  }

  private async attempt<T>(
    path: string,
    method: string,
    options: RequestOptions,
  ): Promise<ClientResponse<T>> {
    const fetchImpl = this.options.fetchImpl ?? globalThis.fetch;
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Client-Request-ID': nextRequestId(),
    };
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
    if (options.expectedRevision !== undefined) {
      headers['If-Match'] = String(options.expectedRevision);
    }
    if (method !== 'GET' && options.csrfToken) headers['X-CSRF-Token'] = options.csrfToken;

    const startedAt = Date.now();
    const response = await fetchImpl(`${this.options.baseUrl ?? ''}${path}`, {
      method,
      headers,
      credentials: 'same-origin',
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });

    const traceId = response.headers.get('X-Trace-ID');
    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }
    this.options.onEvent?.({
      method,
      path,
      status: response.status,
      durationMs: Date.now() - startedAt,
      traceId: traceId ?? null,
    });

    if (!response.ok) {
      throw normaliseError(response.status, parsed, traceId);
    }

    const body = (parsed ?? {}) as Record<string, unknown>;
    const meta = (body.meta ?? {}) as Record<string, unknown>;
    return {
      data: (body.data ?? body) as T,
      traceId: traceId ?? (typeof meta.trace_id === 'string' ? meta.trace_id : ''),
      snapshotId: typeof meta.snapshot_id === 'string' ? meta.snapshot_id : null,
      partial: meta.partial === true,
      warnings: Array.isArray(meta.warnings)
        ? (meta.warnings as Array<{ code: string; message: string }>)
        : [],
    };
  }

  private async sleep(ms: number): Promise<void> {
    if (this.options.sleepImpl) return this.options.sleepImpl(ms);
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /**
   * One multipart file upload (spec docs/06 §导入).
   *
   * Never retried automatically: the server decides idempotency from the
   * per-file key, and a retry of a big upload is the user's decision. The
   * filename is NOT trusted for anything but display — the server keeps its own
   * batch/item ids.
   */
  async upload<T>(path: string, file: File, options: UploadOptions = {}): Promise<ClientResponse<T>> {
    const fetchImpl = this.options.fetchImpl ?? globalThis.fetch;
    const form = new FormData();
    form.append('file', file, file.name);
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'X-Client-Request-ID': nextRequestId(),
    };
    if (options.csrfToken) headers['X-CSRF-Token'] = options.csrfToken;

    const response = await fetchImpl(`${this.options.baseUrl ?? ''}${path}`, {
      method: 'POST',
      headers,
      credentials: 'same-origin',
      body: form,
      signal: options.signal,
    });

    const traceId = response.headers.get('X-Trace-ID');
    const text = await response.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }
    if (!response.ok) throw normaliseError(response.status, parsed, traceId);

    const body = (parsed ?? {}) as Record<string, unknown>;
    const meta = (body.meta ?? {}) as Record<string, unknown>;
    return {
      data: (body.data ?? body) as T,
      traceId: traceId ?? '',
      snapshotId: typeof meta.snapshot_id === 'string' ? meta.snapshot_id : null,
      partial: meta.partial === true,
      warnings: [],
    };
  }
}
