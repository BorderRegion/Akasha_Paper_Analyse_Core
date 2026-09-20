import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../../src/api/client';
import { ApiError } from '../../src/api/errors';
import { clearSchemaIssues, schemaIssues, validateEnvelope, validatePage } from '../../src/api/validate';

function jsonResponse(body: unknown, init: { status?: number; headers?: Record<string, string> } = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
  });
}

const noSleep = () => Promise.resolve();

describe('UI data path contract (F02 groundwork)', () => {
  it('sends the client request id and reads the trace id back', async () => {
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      expect(headers['X-Client-Request-ID']).toMatch(/^ui-/);
      return jsonResponse(
        { data: { ok: true }, meta: { contract_version: '1.0.0', snapshot_id: 's1', observed_at: 'now', partial: false, warnings: [] } },
        { headers: { 'X-Trace-ID': 'trc_test' } },
      );
    });
    const client = new ApiClient({ mode: 'LIVE', fetchImpl: fetchImpl as unknown as typeof fetch });
    const response = await client.request<{ ok: boolean }>('/v1/ui/bootstrap');
    expect(response.traceId).toBe('trc_test');
    expect(response.snapshotId).toBe('s1');
    expect(response.partial).toBe(false);
  });

  it('does not retry 401/403/409/412/422', async () => {
    for (const status of [401, 403, 409, 412, 422]) {
      const fetchImpl = vi.fn(async () =>
        jsonResponse({ error: { code: 'PERMISSION_DENIED', message: 'nope' } }, { status }),
      );
      const client = new ApiClient({
        mode: 'LIVE',
        fetchImpl: fetchImpl as unknown as typeof fetch,
        sleepImpl: noSleep,
      });
      await expect(client.request('/v1/ui/bootstrap')).rejects.toBeInstanceOf(ApiError);
      expect(fetchImpl).toHaveBeenCalledTimes(1);
    }
  });

  it('retries a retryable transport failure at most twice', async () => {
    const fetchImpl = vi.fn(async () =>
      jsonResponse({ error: { code: 'INTERNAL_001', message: 'boom' } }, { status: 500 }),
    );
    const client = new ApiClient({
      mode: 'LIVE',
      fetchImpl: fetchImpl as unknown as typeof fetch,
      sleepImpl: noSleep,
    });
    await expect(client.request('/v1/ui/operations/snapshot')).rejects.toBeInstanceOf(ApiError);
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it('normalises the three error shapes the backend can emit', async () => {
    const cases: Array<[number, unknown, string]> = [
      [400, { error: { code: 'CFG_002', message: 'bad', retryable: false, trace_id: 'trc_1', details: {} } }, 'ui-envelope'],
      [400, { error: { code: 'CFG_002', message: 'bad', details: {} } }, 'domain-error'],
      [422, { detail: [{ loc: ['body', 'limit'], msg: 'too big', type: 'value_error' }] }, 'validation-422'],
    ];
    for (const [status, body, source] of cases) {
      const fetchImpl = vi.fn(async () => jsonResponse(body, { status }));
      const client = new ApiClient({ mode: 'LIVE', fetchImpl: fetchImpl as unknown as typeof fetch, sleepImpl: noSleep });
      try {
        await client.request('/v1/ui/library/query', { method: 'POST', body: {} });
        throw new Error('expected failure');
      } catch (error) {
        expect(error).toBeInstanceOf(ApiError);
        expect((error as ApiError).shape.source).toBe(source);
      }
    }
  });

  it('sends CSRF and idempotency headers on writes only', async () => {
    const seen: Record<string, string>[] = [];
    const fetchImpl = vi.fn(async (_url: string, init?: RequestInit) => {
      seen.push((init?.headers ?? {}) as Record<string, string>);
      return jsonResponse({ data: {}, meta: { contract_version: '1.0.0', snapshot_id: 's', observed_at: 'n', partial: false, warnings: [] } });
    });
    const client = new ApiClient({ mode: 'LIVE', fetchImpl: fetchImpl as unknown as typeof fetch });
    await client.request('/v1/ui/bootstrap');
    expect(seen[0]?.['X-CSRF-Token']).toBeUndefined();
    expect(seen[0]?.['Idempotency-Key']).toBeUndefined();

    await client.request('/v1/ui/notes', {
      method: 'POST',
      body: { body: 'x' },
      csrfToken: 'csrf-1',
      idempotencyKey: 'idem-1',
      expectedRevision: 3,
    });
    expect(seen[1]?.['X-CSRF-Token']).toBe('csrf-1');
    expect(seen[1]?.['Idempotency-Key']).toBe('idem-1');
    expect(seen[1]?.['If-Match']).toBe('3');
  });

  it('records envelope and page schema violations instead of casting them away', () => {
    clearSchemaIssues();
    const badEnvelope = validateEnvelope({ data: {}, meta: { contract_version: '9.9.9' } });
    expect(badEnvelope.ok).toBe(false);
    expect(schemaIssues().some((issue) => issue.path === '$.meta.contract_version')).toBe(true);

    const badPage = validatePage({ items: 'nope', has_more: 'yes' });
    expect(badPage.ok).toBe(false);
    expect(schemaIssues().some((issue) => issue.path === '$.data.items')).toBe(true);
    clearSchemaIssues();
  });

  it('accepts a well-formed envelope and page', () => {
    clearSchemaIssues();
    const envelope = validateEnvelope({
      data: { x: 1 },
      meta: { contract_version: '1.0.0', snapshot_id: 's', observed_at: 't', partial: false, warnings: [] },
    });
    expect(envelope.ok).toBe(true);
    const page = validatePage({ items: [], next_cursor: null, has_more: false, total: null, total_kind: 'UNKNOWN' });
    expect(page.ok).toBe(true);
    expect(schemaIssues()).toHaveLength(0);
  });
});
