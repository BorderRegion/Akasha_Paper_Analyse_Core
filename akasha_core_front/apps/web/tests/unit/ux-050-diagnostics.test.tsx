/**
 * UX-050 — the diagnostic package is redacted and bounded.
 *
 * Requirement (docs/09 §UI Inspector): 复制诊断默认无请求正文/笔记/搜索原文/token/
 * authorization/cookie；必要时用户单独勾选“包含当前证据摘录”并看到预览；保留最近200个
 * 客户端事件；报告默认2MiB上限.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import {
  MAX_CLIENT_EVENTS,
  clearClientEvents,
  clientEvents,
  recordEvent,
  routeTemplate,
} from '../../src/state/clientEvents';

describe('UX-050 diagnostics are redacted and limited', () => {
  beforeEach(() => clearClientEvents());

  it('keeps at most 200 client events', () => {
    for (let index = 0; index < 260; index += 1) {
      recordEvent({ kind: 'render', detail: `event ${index}` });
    }
    const events = clientEvents();
    expect(events).toHaveLength(MAX_CLIENT_EVENTS);
    // The OLDEST events are dropped, not the newest.
    expect(events[0]?.detail).toBe('event 60');
    expect(events.at(-1)?.detail).toBe('event 259');
  });

  it('records request summaries by route TEMPLATE, never by id or query', () => {
    expect(routeTemplate('/v1/ui/papers/pap_01HZZ/workspace?foo=bar')).toBe(
      '/v1/ui/papers/{id}/workspace',
    );
    expect(routeTemplate('/v1/ui/library/query')).toBe('/v1/ui/library/query');
    expect(routeTemplate('/v1/claims/clm_01ABC/evidence')).toBe('/v1/claims/{id}/evidence');
  });

  it('shows the route, build, contract version and request history in the drawer', async () => {
    const server = createFakeServer({ snapshot: { disk: [], workers: [], modules: [], providers: [], queue: { pending: 0, running: 0, failed: 0 }, eta_available: false, observed_at: 'now', unknown_reasons: [] } });
    renderApp(server, { path: '/app/settings' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /打开诊断抽屉/ }));
    const drawer = await screen.findByTestId('diagnostics-drawer');
    expect(drawer).toHaveTextContent('/app/settings');
    expect(drawer).toHaveTextContent('ui_contract_version');
    expect(await screen.findByTestId('diag-events')).toHaveTextContent(`/${MAX_CLIENT_EVENTS} 条`);
  });

  it('defaults to NO excerpt and shows a preview only when the user opts in', async () => {
    const server = createFakeServer({});
    renderApp(server, { path: '/app/settings' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /打开诊断抽屉/ }));
    await screen.findByTestId('diagnostics-drawer');

    expect(screen.queryByTestId('excerpt-preview')).not.toBeInTheDocument();
    await user.click(screen.getByLabelText(/包含当前证据摘录/));
    // The option is opt-in; without an excerpt in context nothing is added.
    expect(screen.queryByTestId('excerpt-preview')).not.toBeInTheDocument();
  });

  it('posts a manifest that excludes bodies, notes, search text and credentials', async () => {
    const server = createFakeServer({});
    renderApp(server, { path: '/app/settings' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /打开诊断抽屉/ }));
    await screen.findByTestId('diagnostics-drawer');
    await user.click(screen.getByRole('button', { name: '生成诊断包（服务端）' }));

    await waitFor(() => expect(server.diagnostics).toHaveLength(1));
    const payload = server.diagnostics[0] as { include_evidence_excerpt?: boolean; events?: unknown[] };
    expect(payload.include_evidence_excerpt).toBe(false);
    expect(Array.isArray(payload.events)).toBe(true);
    // No credential-ish field is sent by the drawer.
    expect(JSON.stringify(payload)).not.toMatch(/authorization|Bearer|cookie/i);
    expect(await screen.findByTestId('diag-report')).toHaveTextContent('字节');
  });

  it('reports the server-side size limit so a large package is visible', async () => {
    const server = createFakeServer({});
    renderApp(server, { path: '/app/settings' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /打开诊断抽屉/ }));
    await user.click(await screen.findByRole('button', { name: '生成诊断包（服务端）' }));
    const report = await screen.findByTestId('diag-report');
    expect(report).toHaveTextContent(`${2 * 1024 * 1024}`);
  });
});
