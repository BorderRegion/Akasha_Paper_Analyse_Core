/**
 * Runtime contract validation (spec docs/08: DTO → generated TS → runtime
 * schema check → adapter → ViewModel).
 *
 * Hand-written validators with no extra dependency: they check the shapes the
 * UI actually depends on and RECORD every failure (schema_errors in the debug
 * inspector) instead of casting with `as any`. A validation failure surfaces as
 * `partial`/`error` with a locating path.
 */

export interface SchemaIssue {
  path: string;
  expected: string;
  actual: string;
}

export type ValidationResult<T> =
  | { ok: true; value: T }
  | { ok: false; issues: SchemaIssue[] };

const recorded: SchemaIssue[] = [];

export function recordIssues(issues: SchemaIssue[]): void {
  recorded.push(...issues);
}

export function schemaIssues(): SchemaIssue[] {
  return [...recorded];
}

export function clearSchemaIssues(): void {
  recorded.length = 0;
}

function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Envelope + meta (docs/06 「通用响应」). */
export function validateEnvelope(
  payload: unknown,
): ValidationResult<{ data: unknown; meta: Record<string, unknown> }> {
  const issues: SchemaIssue[] = [];
  if (!isRecord(payload)) {
    return { ok: false, issues: [{ path: '$', expected: 'object', actual: typeName(payload) }] };
  }
  if (!('data' in payload)) issues.push({ path: '$.data', expected: 'present', actual: 'missing' });
  const meta = payload.meta;
  if (!isRecord(meta)) {
    issues.push({ path: '$.meta', expected: 'object', actual: typeName(meta) });
  } else {
    if (meta.contract_version !== '1.0.0') {
      issues.push({
        path: '$.meta.contract_version',
        expected: '1.0.0',
        actual: String(meta.contract_version),
      });
    }
    for (const key of ['snapshot_id', 'observed_at'] as const) {
      if (typeof meta[key] !== 'string') {
        issues.push({ path: `$.meta.${key}`, expected: 'string', actual: typeName(meta[key]) });
      }
    }
    if (typeof meta.partial !== 'boolean') {
      issues.push({ path: '$.meta.partial', expected: 'boolean', actual: typeName(meta.partial) });
    }
    if (!Array.isArray(meta.warnings)) {
      issues.push({ path: '$.meta.warnings', expected: 'array', actual: typeName(meta.warnings) });
    }
  }
  if (issues.length > 0) {
    recordIssues(issues);
    return { ok: false, issues };
  }
  return { ok: true, value: { data: payload.data, meta: meta as Record<string, unknown> } };
}

/** Cursor page (docs/06 「分页」). */
export function validatePage(
  data: unknown,
): ValidationResult<{ items: unknown[]; next_cursor: string | null; has_more: boolean }> {
  const issues: SchemaIssue[] = [];
  if (!isRecord(data)) {
    return { ok: false, issues: [{ path: '$.data', expected: 'object', actual: typeName(data) }] };
  }
  if (!Array.isArray(data.items)) {
    issues.push({ path: '$.data.items', expected: 'array', actual: typeName(data.items) });
  }
  if (data.next_cursor !== null && typeof data.next_cursor !== 'string') {
    issues.push({
      path: '$.data.next_cursor',
      expected: 'string|null',
      actual: typeName(data.next_cursor),
    });
  }
  if (typeof data.has_more !== 'boolean') {
    issues.push({ path: '$.data.has_more', expected: 'boolean', actual: typeName(data.has_more) });
  }
  if (issues.length > 0) {
    recordIssues(issues);
    return { ok: false, issues };
  }
  return {
    ok: true,
    value: {
      items: data.items as unknown[],
      next_cursor: (data.next_cursor as string | null) ?? null,
      has_more: data.has_more as boolean,
    },
  };
}

/** Capabilities (docs/06 「会话与能力」). */
export function validateCapabilities(payload: unknown): ValidationResult<{
  ui_contract_version: string;
  mode: string;
  limits: Record<string, number>;
  capabilities: Array<{ code: string; availability: string; reason: string | null }>;
}> {
  const issues: SchemaIssue[] = [];
  if (!isRecord(payload)) {
    return { ok: false, issues: [{ path: '$', expected: 'object', actual: typeName(payload) }] };
  }
  if (payload.ui_contract_version !== '1.0.0') {
    issues.push({
      path: '$.ui_contract_version',
      expected: '1.0.0',
      actual: String(payload.ui_contract_version),
    });
  }
  if (payload.mode !== 'LIVE' && payload.mode !== 'TEST') {
    issues.push({ path: '$.mode', expected: 'LIVE|TEST', actual: String(payload.mode) });
  }
  const limits = payload.limits;
  if (!isRecord(limits)) {
    issues.push({ path: '$.limits', expected: 'object', actual: typeName(limits) });
  } else {
    for (const key of ['upload_file_bytes', 'upload_batch_bytes', 'upload_files', 'library_page_size'] as const) {
      if (typeof limits[key] !== 'number') {
        issues.push({ path: `$.limits.${key}`, expected: 'number', actual: typeName(limits[key]) });
      }
    }
  }
  if (!Array.isArray(payload.capabilities)) {
    issues.push({
      path: '$.capabilities',
      expected: 'array',
      actual: typeName(payload.capabilities),
    });
  } else {
    payload.capabilities.forEach((entry, index) => {
      if (!isRecord(entry) || typeof entry.code !== 'string') {
        issues.push({
          path: `$.capabilities[${index}].code`,
          expected: 'string',
          actual: typeName(isRecord(entry) ? entry.code : entry),
        });
      }
    });
  }
  if (issues.length > 0) {
    recordIssues(issues);
    return { ok: false, issues };
  }
  return {
    ok: true,
    value: {
      ui_contract_version: '1.0.0',
      mode: payload.mode as string,
      limits: limits as Record<string, number>,
      capabilities: payload.capabilities as Array<{
        code: string;
        availability: string;
        reason: string | null;
      }>,
    },
  };
}
