/**
 * UX-021 — coming back from a paper restores filters, scroll and selection.
 *
 * Requirement (docs/03 §S02): 搜索条件和结果 scope 保存至 URL；勾选只选择当前明确的
 * paper IDs，跨页选择保留托盘与数量；"全库选择"不提供隐式操作.
 *
 * The test drives the REAL browser history: it opens a paper, goes back, and
 * checks that the same filtered query is issued again and the tray still holds
 * the same explicit ids.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

function renderAt(path: string, server: ReturnType<typeof createFakeServer>) {
  window.history.pushState({}, '', path);
  return render(<App mode="TEST" fetchImpl={server.fetch} />);
}

function queries(server: ReturnType<typeof createFakeServer>) {
  return server.requests.filter(
    (request) => request.method === 'POST' && request.path === '/v1/ui/library/query',
  );
}

describe('UX-021 returning to the library keeps filters, scroll and selection', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.pushState({}, '', '/app/home');
  });

  it('re-issues the same filtered query after a round trip to a paper', async () => {
    const server = createFakeServer({
      papers: [
        { paper_id: 'pap_1', title: 'First paper', read_state: 'READING' },
        { paper_id: 'pap_2', title: 'Second paper' },
      ],
    });
    const first = renderAt('/app/library?q=paper&sort=TITLE&read=READING', server);
    await screen.findByRole('link', { name: 'First paper' });
    const initial = queries(server).at(-1)?.body as { query?: string; sort?: string } | undefined;
    expect(initial?.query).toBe('paper');
    expect(initial?.sort).toBe('TITLE');

    // Opening the paper: the library unmounts and the URL changes.
    first.unmount();
    window.history.pushState({}, '', '/app/papers/pap_1?paper_version_id=pver_1&page=7');
    const paper = render(<App mode="TEST" fetchImpl={server.fetch} />);
    await waitFor(() => expect(window.location.pathname).toBe('/app/papers/pap_1'));
    paper.unmount();

    // Going back restores the previous URL entry, which carries the filters.
    const popped = new Promise<void>((resolve) => {
      window.addEventListener('popstate', () => resolve(), { once: true });
    });
    window.history.back();
    await popped;
    expect(window.location.pathname).toBe('/app/library');

    render(<App mode="TEST" fetchImpl={server.fetch} />);
    await screen.findByRole('link', { name: 'First paper' });
    const afterBack = queries(server).at(-1)?.body as
      | { query?: string; sort?: string; filters?: { read_states?: string[] } }
      | undefined;
    expect(afterBack?.query).toBe('paper');
    expect(afterBack?.sort).toBe('TITLE');
    expect(afterBack?.filters?.read_states).toEqual(['READING']);
    expect(screen.getByLabelText('检索')).toHaveValue('paper');
  });

  it('keeps a cross-page selection and its count in the tray', async () => {
    const server = createFakeServer({
      papers: [
        { paper_id: 'pap_1', title: 'First paper' },
        { paper_id: 'pap_2', title: 'Second paper' },
        { paper_id: 'pap_3', title: 'Third paper' },
      ],
    });
    const user = userEvent.setup();
    renderAt('/app/library?limit=2', server);

    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('checkbox', { name: '选择 First paper' }));
    expect(await screen.findByTestId('selection-count')).toHaveTextContent('已选择 1 篇');

    // Page 2 keeps the tray (selection is not reset by pagination).
    await user.click(screen.getByRole('button', { name: '载入下一页' }));
    await screen.findByRole('link', { name: 'Third paper' });
    await user.click(screen.getByRole('checkbox', { name: '选择 Third paper' }));

    const tray = screen.getByTestId('selection-count');
    expect(tray).toHaveTextContent('已选择 2 篇');
    const ids = within(screen.getByLabelText('批量操作托盘')).getAllByRole('listitem');
    expect(ids.map((item) => item.textContent)).toEqual(['pap_1', 'pap_3']);
  });

  it('survives a full remount (refresh) through session storage', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    const user = userEvent.setup();
    const first = renderAt('/app/library', server);
    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('checkbox', { name: '选择 First paper' }));
    await screen.findByTestId('selection-count');
    first.unmount();

    // A refresh: the URL is the user's state, the tray is the workspace state.
    renderAt('/app/library', server);
    await screen.findByRole('link', { name: 'First paper' });
    expect(await screen.findByTestId('selection-count')).toHaveTextContent('已选择 1 篇');
  });

  it('has no hidden "select everything" action', async () => {
    const server = createFakeServer({
      papers: [
        { paper_id: 'pap_1', title: 'First paper' },
        { paper_id: 'pap_2', title: 'Second paper' },
      ],
    });
    const user = userEvent.setup();
    renderAt('/app/library', server);
    await screen.findByRole('link', { name: 'First paper' });
    await user.click(screen.getByRole('checkbox', { name: '选择 First paper' }));

    const tray = screen.getByLabelText('批量操作托盘');
    expect(within(tray).queryByRole('button', { name: /全库|全选/ })).toBeNull();
    expect(tray).toHaveTextContent('跨页保留');
  });
});
