/**
 * UX-024 — favourites: instant, rolled back on failure, and server-truth on refresh.
 *
 * Requirement (docs/05 §Mutation协议): 收藏等可撤销操作可乐观更新；失败回滚并保留草稿.
 * The optimistic value must never become the displayed truth after a failure,
 * and a refresh must show what the SERVER holds (not the local guess).
 */

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

function renderLibrary(server: ReturnType<typeof createFakeServer>, search = '') {
  window.history.pushState({}, '', `/app/library${search}`);
  return render(<App mode="TEST" fetchImpl={server.fetch} />);
}

function savedState(paperId: string): string {
  const row = document.querySelector(`li[data-paper-id="${paperId}"]`);
  return row?.querySelector('[data-testid="saved-state"]')?.textContent ?? '';
}

describe('UX-024 favourite toggles optimistically, rolls back, and keeps server truth', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
  });

  it('shows the new state immediately and persists it on the server', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    renderLibrary(server);
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'First paper' });
    expect(savedState('pap_1')).toBe('未收藏');

    await user.click(screen.getByRole('button', { name: '收藏' }));
    await waitFor(() => expect(savedState('pap_1')).toBe('已收藏'));
    await waitFor(() => expect(server.personalPatches).toHaveLength(1));
    expect(server.personalPatches[0]).toMatchObject({ paperId: 'pap_1' });
    expect(server.personalPatches[0]?.body).toMatchObject({ saved: true });
  });

  it('rolls back and explains when the save fails', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'First paper' }],
      failures: {
        'PATCH /v1/ui/papers/pap_1/personal': {
          status: 409,
          code: 'REVISION_CONFLICT',
          message: 'Personal state changed.',
        },
      },
    });
    renderLibrary(server);
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('button', { name: '收藏' }));

    const error = await screen.findByTestId('save-error');
    expect(error).toHaveTextContent('REVISION_CONFLICT');
    expect(error).toHaveTextContent('已恢复原状态');
    await waitFor(() => expect(savedState('pap_1')).toBe('未收藏'));
  });

  it('sends the revision it saw, so a lost race is refused rather than overwritten', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'First paper', revision: 4 }],
    });
    renderLibrary(server);
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('button', { name: '收藏' }));

    await waitFor(() => expect(server.personalPatches).toHaveLength(1));
    expect(server.personalPatches[0]?.body.expected_revision).toBe(4);
  });

  it('shows the server value after a refresh, not the last local guess', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    const user = userEvent.setup();
    const first = renderLibrary(server);
    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('button', { name: '收藏' }));
    await waitFor(() => expect(savedState('pap_1')).toBe('已收藏'));
    first.unmount();

    // Another client clears the favourite in the meantime.
    server.papers[0]!.personal.saved = false;
    server.papers[0]!.personal.revision += 1;

    renderLibrary(server);
    await screen.findByRole('link', { name: 'First paper' });
    await waitFor(() => expect(savedState('pap_1')).toBe('未收藏'));
  });

  it('never claims success while the write is still in flight', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = () => resolve();
    });
    const original = server.fetch;
    server.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'PATCH') await gate;
      return original(input as never, init);
    }) as unknown as typeof fetch;

    renderLibrary(server);
    const user = userEvent.setup();
    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('button', { name: '收藏' }));

    const toggle = screen.getByRole('button', { name: '取消收藏' });
    expect(toggle).toBeDisabled();
    release();
    await waitFor(() => expect(server.personalPatches).toHaveLength(1));
  });
});
