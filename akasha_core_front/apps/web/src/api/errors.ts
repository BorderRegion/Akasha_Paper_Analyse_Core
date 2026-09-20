/**
 * Error handling for the UI data path (spec docs/06 「错误」).
 *
 * The UI must present either the new UI envelope or the legacy DomainError /
 * FastAPI-422 shapes the same way: one normalised object, with the trace id
 * kept for the DebugInspector. Codes are never invented.
 */

export interface UiErrorShape {
  code: string;
  message: string;
  retryable: boolean;
  trace_id: string;
  details: Record<string, unknown>;
  /** Where the error was produced, for debugging (not shown as content). */
  source: 'ui-envelope' | 'domain-error' | 'validation-422' | 'transport';
  http_status: number;
}

export class ApiError extends Error {
  readonly shape: UiErrorShape;

  constructor(shape: UiErrorShape) {
    super(`${shape.code}: ${shape.message}`);
    this.name = 'ApiError';
    this.shape = shape;
  }

  get retryable(): boolean {
    return this.shape.retryable;
  }

  get code(): string {
    return this.shape.code;
  }
}

/** Statuses that must never be retried automatically (docs/08 §缓存与更新). */
export const NON_RETRYABLE_STATUSES = [401, 403, 409, 412, 422] as const;

export function isNonRetryable(status: number): boolean {
  return (NON_RETRYABLE_STATUSES as readonly number[]).includes(status);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

export function normaliseError(status: number, body: unknown, traceId: string | null): ApiError {
  const record = asRecord(body);
  const envelope = asRecord(record.error);

  // 1. New UI envelope: {error:{code,message,retryable,trace_id,details}}.
  // The explicit boolean `retryable` is what distinguishes it from the legacy
  // DomainError envelope, which carries no retry semantics.
  if (typeof envelope.code === 'string' && typeof envelope.retryable === 'boolean') {
    return new ApiError({
      code: envelope.code,
      message: typeof envelope.message === 'string' ? envelope.message : '',
      retryable: envelope.retryable === true,
      trace_id: typeof envelope.trace_id === 'string' ? envelope.trace_id : (traceId ?? ''),
      details: asRecord(envelope.details),
      source: 'ui-envelope',
      http_status: status,
    });
  }

  // 2. Legacy DomainError envelope ({"error":{code,message,details}})
  if (typeof envelope.code === 'string' || typeof record.code === 'string') {
    const code = typeof envelope.code === 'string' ? envelope.code : String(record.code);
    return new ApiError({
      code,
      message:
        typeof envelope.message === 'string'
          ? envelope.message
          : typeof record.message === 'string'
            ? record.message
            : '',
      // Only transport-shaped codes are retryable; a domain rejection is not.
      retryable: !isNonRetryable(status) && status >= 500,
      trace_id: traceId ?? '',
      details: asRecord(envelope.details),
      source: 'domain-error',
      http_status: status,
    });
  }

  // 3. FastAPI validation errors: {detail:[{loc,msg,type}]}
  if (Array.isArray(record.detail)) {
    return new ApiError({
      code: 'VALIDATION_422',
      message: record.detail
        .map((item) => {
          const entry = asRecord(item);
          const loc = Array.isArray(entry.loc) ? entry.loc.join('.') : '';
          return `${loc}: ${String(entry.msg ?? '')}`;
        })
        .join('; '),
      retryable: false,
      trace_id: traceId ?? '',
      details: { detail: record.detail },
      source: 'validation-422',
      http_status: status,
    });
  }

  return new ApiError({
    code: status >= 500 ? 'INTERNAL_001' : 'REQUEST_FAILED',
    message: typeof record.detail === 'string' ? record.detail : '请求失败',
    retryable: status >= 500,
    trace_id: traceId ?? '',
    details: {},
    source: 'transport',
    http_status: status,
  });
}
