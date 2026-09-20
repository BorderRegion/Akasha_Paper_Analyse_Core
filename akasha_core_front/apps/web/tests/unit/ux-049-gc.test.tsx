/**
 * UX-049 — GC previews first and re-checks references before deleting.
 *
 * Requirement (docs/09 §GC与数据安全 + docs/03 §S10): 先预览 {candidate_ids,total_bytes,
 * scope_hash,expires_at} 再确认；执行时二次检查活动任务引用和对象引用；scope过期409重算；
 * 普通GC不删PDF、证据、笔记.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const SNAPSHOT = {
  queue: { pending: 0, running: 0, failed: 0 },
  workers: [{ identity: '(none)', last_seen: null, state: 'UNOBSERVED' }],
  modules: [],
  providers: [],
  disk: [],
  eta_available: false,
  observed_at: new Date().toISOString(),
  unknown_reasons: [],
};

const PREVIEW_RESULTS = [
  { target_id: '/data/temp/old.part', status: '1024 bytes (TEMP)', error_code: null, reason: 'stale temp file' },
  { target_id: '/data/cache/report.json', status: '2048 bytes (CACHE)', error_code: null, reason: 'cache rebuildable' },
  { target_id: 'SCOPE', status: 'scopehash123', error_code: null, reason: '2026-09-19T00:00:00+00:00' },
];

function previewServer() {
  const server = createFakeServer({ snapshot: SNAPSHOT });
  const original = server.fetch;
  server.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    if (String(input).includes('/v1/ui/operations') && body?.kind === 'gc_preview') {
      server.operations.push({ kind: 'gc_preview', payload: body.payload ?? {}, idempotencyKey: null });
      return new Response(
        JSON.stringify({
          data: {
            operation_id: 'uop_preview_1',
            kind: 'gc_preview',
            state: 'COMPLETED',
            scope: {},
            job_ids: [],
            results: PREVIEW_RESULTS,
            error_code: null,
          },
          meta: { contract_version: '1.0.0', snapshot_id: 's', observed_at: 'now', partial: false, warnings: [] },
        }),
        { status: 202, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
      );
    }
    if (String(input).includes('/v1/ui/operations') && body?.kind === 'gc_execute') {
      server.operations.push({ kind: 'gc_execute', payload: body.payload ?? {}, idempotencyKey: null });
      return new Response(
        JSON.stringify({
          data: {
            operation_id: 'uop_exec_1',
            kind: 'gc_execute',
            state: 'COMPLETED',
            scope: {},
            job_ids: [],
            results: [{ target_id: '/data/temp/old.part', status: 'REMOVED', error_code: null }],
            error_code: null,
          },
          meta: { contract_version: '1.0.0', snapshot_id: 's', observed_at: 'now', partial: false, warnings: [] },
        }),
        { status: 202, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
      );
    }
    return original(input as never, init);
  }) as unknown as typeof fetch;
  return server;
}

describe('UX-049 GC previews before deleting and rechecks the scope', () => {
  it('previews candidates with the scope hash and expiry, deleting nothing', async () => {
    const server = previewServer();
    renderApp(server, { path: '/app/operations' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '预览可清理对象' }));
    const preview = await screen.findByTestId('gc-preview');
    expect(preview).toHaveTextContent('候选 2 个对象');
    expect(preview).toHaveTextContent('scopehash123');
    expect(preview).toHaveTextContent('有效期至');
    // Only the preview ran: nothing was deleted.
    expect(server.operations.map((operation) => operation.kind)).toEqual(['gc_preview']);
  });

  it('sends the preview id AND the scope hash when confirming', async () => {
    const server = previewServer();
    renderApp(server, { path: '/app/operations' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '预览可清理对象' }));
    await user.click(await screen.findByRole('button', { name: '确认清理这些对象' }));

    await waitFor(() => expect(server.operations.some((op) => op.kind === 'gc_execute')).toBe(true));
    const executed = server.operations.find((operation) => operation.kind === 'gc_execute');
    expect(executed?.payload.preview_id).toBe('uop_preview_1');
    expect(executed?.payload.scope_hash).toBe('scopehash123');
    expect(await screen.findByTestId('gc-result')).toHaveTextContent('清理完成');
  });

  it('explains an expired scope instead of deleting', async () => {
    const server = createFakeServer({
      snapshot: SNAPSHOT,
      failures: {
        'POST /v1/ui/operations': {
          status: 409,
          code: 'SCOPE_EXPIRED',
          message: '该预览已过期，请重新预览后再执行。',
        },
      },
    });
    renderApp(server, { path: '/app/operations' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '预览可清理对象' }));
    const error = await screen.findByTestId('gc-error');
    expect(error).toHaveTextContent('SCOPE_EXPIRED');
    expect(error).toHaveTextContent('必须重新预览');
    expect(screen.queryByTestId('gc-result')).not.toBeInTheDocument();
  });

  it('states that ordinary GC never removes PDFs, evidence or notes', async () => {
    const server = previewServer();
    renderApp(server, { path: '/app/operations' });
    expect(await screen.findByText(/不会删除 PDF、证据或笔记/)).toBeInTheDocument();
  });
});
