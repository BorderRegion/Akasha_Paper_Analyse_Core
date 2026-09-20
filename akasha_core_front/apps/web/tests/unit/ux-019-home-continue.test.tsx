/**
 * UX-019 — the home page continues where the reader actually stopped.
 *
 * Requirement (docs/03 §S01): "接着上次读"最多3篇, 数据来自个人阅读定位，
 * 不根据任务阶段推断; the continue action opens the STORED version and page; an
 * empty library offers an import button and one line of explanation; a library
 * without reading history says "开始一篇" instead of a fake "welcome back".
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

function renderHome(server: ReturnType<typeof createFakeServer>) {
  window.history.pushState({}, '', '/app/home');
  return render(<App mode="TEST" fetchImpl={server.fetch} />);
}

describe('UX-019 home restores the reading version and position', () => {
  it('does not present a failed initial read as a true zero or empty history', async () => {
    const server = createFakeServer({ failures: {
      'POST /v1/ui/library/query': { status: 403, code: 'AUTH_001', message: 'forbidden' },
    } });
    renderHome(server);
    await screen.findAllByRole('alert');
    expect(screen.queryByTestId('attention-empty')).not.toBeInTheDocument();
    expect(screen.queryByTestId('no-reading-history')).not.toBeInTheDocument();
    expect(screen.queryByTestId('empty-library')).not.toBeInTheDocument();
  });
  it('shows at most three resumes, each with its stored version and page', async () => {
    const server = createFakeServer({
      papers: [
        {
          paper_id: 'pap_1',
          title: 'First paper',
          read_state: 'READING',
          page_number: 7,
          anchor_version: 'pver_old',
        },
        { paper_id: 'pap_2', title: 'Second paper', read_state: 'READ', page_number: 3 },
        { paper_id: 'pap_3', title: 'Third paper', read_state: 'READING', page_number: 1 },
        { paper_id: 'pap_4', title: 'Fourth paper', read_state: 'READING', page_number: 2 },
      ],
    });
    renderHome(server);

    const heading = await screen.findByRole('heading', { name: '接着上次读' });
    const block = heading.closest('section') as HTMLElement;
    await waitFor(() => expect(within(block).getAllByRole('link', { name: /继续阅读/ }).length).toBe(3), {
      timeout: 3000,
    });
    expect(within(block).queryByText('Fourth paper')).not.toBeInTheDocument();

    const resume = within(block).getAllByRole('link', { name: /继续阅读/ })[0];
    // The ANCHOR version is used, not the newest version of the paper.
    expect(resume).toHaveAttribute(
      'href',
      '/app/papers/pap_1?paper_version_id=pver_old&reader=1&page=7',
    );
    expect(within(block).getByText(/上次读到第 7 页/)).toBeInTheDocument();
  });

  it('offers an import action when the library is empty and never fakes a welcome back', async () => {
    const server = createFakeServer({ papers: [] });
    renderHome(server);

    const empty = await screen.findByTestId('empty-library');
    expect(empty).toHaveTextContent('文献库还是空的');
    expect(screen.getByRole('button', { name: '导入 PDF' })).toBeInTheDocument();
    expect(screen.queryByText(/欢迎回来/)).not.toBeInTheDocument();
  });

  it('says "开始一篇" when the library has papers but no reading history', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Untouched paper' }] });
    renderHome(server);

    const block = await screen.findByTestId('no-reading-history');
    expect(within(block).getByRole('link', { name: /开始一篇/ })).toBeInTheDocument();
    expect(screen.queryByText(/欢迎回来/)).not.toBeInTheDocument();
    expect(screen.queryByTestId('empty-library')).not.toBeInTheDocument();
  });

  it('reports a true zero for "需要你判断" instead of hiding the block', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Clean paper' }] });
    renderHome(server);
    expect(await screen.findByTestId('attention-empty')).toHaveTextContent('暂时没有需要你核查的结论');
  });

  it('asks for reading states from the server rather than inferring from job state', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper', read_state: 'READING', page_number: 2 }],
    });
    renderHome(server);
    await screen.findByRole('heading', { name: '接着上次读' });

    const query = server.requests.find(
      (request) => request.method === 'POST' && request.path === '/v1/ui/library/query',
    );
    const body = query?.body as { filters?: { read_states?: string[] } } | undefined;
    expect(body?.filters?.read_states).toEqual(['READING', 'READ']);
  });
});
