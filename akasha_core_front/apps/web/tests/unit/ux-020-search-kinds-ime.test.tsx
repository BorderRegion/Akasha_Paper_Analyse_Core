/**
 * UX-020 — search: result KINDS stay separate, and IME composition is not typed at the server.
 *
 * Requirement (docs/03 §S02): 结果必须分"论文/结论/技巧"，不可将 CLAIM 命中当论文卡；
 * 前端只发送后端支持的过滤项. The input must not query per keystroke while an
 * input method is composing (Chinese/Japanese typing), and a plain keystroke is
 * debounced instead of firing a request per character.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

function renderLibrary(server: ReturnType<typeof createFakeServer>, search = '') {
  window.history.pushState({}, '', `/app/library${search}`);
  return render(<App mode="TEST" fetchImpl={server.fetch} />);
}

function libraryQueries(server: ReturnType<typeof createFakeServer>) {
  return server.requests.filter(
    (request) => request.method === 'POST' && request.path === '/v1/ui/library/query',
  );
}

describe('UX-020 library search keeps result kinds separate and debounces IME input', () => {
  it('renders papers and claims with different rows, never a claim as a paper card', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study' }],
    });
    renderLibrary(server);
    const user = userEvent.setup();

    await screen.findByRole('heading', { name: '文献库' });
    await screen.findByRole('link', { name: 'Detection study' });

    await user.click(screen.getByRole('tab', { name: '结论' }));
    await waitFor(() => {
      const list = document.querySelector('ul[data-kind="CLAIMS"]');
      expect(list).not.toBeNull();
    });
    // A claim hit is not rendered as a paper card (no library row markup).
    expect(document.querySelector('li[data-claim-hit="true"]')).not.toBeNull();
    expect(await screen.findByText('Main stored claim')).toBeVisible();
    expect(document.querySelector('[data-paper-id]')).toBeNull();

    const lastQuery = libraryQueries(server).at(-1)?.body as { kind?: string } | undefined;
    expect(lastQuery?.kind).toBe('CLAIMS');
  });

  it('does not query while an IME composition is in progress, then queries once on commit', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: '检测方法' }] });
    renderLibrary(server);
    const user = userEvent.setup();
    const input = await screen.findByLabelText('检索');

    const before = libraryQueries(server).length;
    await user.click(input);
    // Composition events are what a Chinese IME emits while typing pinyin.
    input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }));
    await user.type(input, 'jiance');
    input.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data: '检测' }));
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(libraryQueries(server).length, 'no request may fire during composition').toBe(before);

    // The browser commits the composed text into the field before
    // compositionend fires; jsdom needs that step spelled out.
    fireEvent.change(input, { target: { value: '检测' } });
    input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '检测' }));
    await waitFor(() => expect(libraryQueries(server).length).toBe(before + 1));
    const body = libraryQueries(server).at(-1)?.body as { query?: string } | undefined;
    expect(body?.query).toBe('检测');
  });

  it('debounces plain typing into a single request', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'abc paper' }] });
    renderLibrary(server);
    const input = await screen.findByLabelText('检索');
    await screen.findByRole('link', { name: 'abc paper' });
    const before = libraryQueries(server).length;

    // A synchronous burst stays inside the debounce window even when other
    // test workers are busy; awaited user.type steps can be >250ms apart.
    fireEvent.change(input, { target: { value: 'a' } });
    fireEvent.change(input, { target: { value: 'ab' } });
    fireEvent.change(input, { target: { value: 'abc' } });
    await waitFor(() => expect(libraryQueries(server).length).toBe(before + 1));
    // Give a generous window: a per-keystroke implementation would show 3.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(libraryQueries(server).length).toBe(before + 1);
  });

  it('keeps the search term and kind in the URL so a reload means the same thing', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Detection study' }] });
    renderLibrary(server, '?q=detect&kind=CLAIMS&sort=TITLE');
    await screen.findByRole('heading', { name: '文献库' });

    const body = libraryQueries(server)[0]?.body as
      | { query?: string; kind?: string; sort?: string }
      | undefined;
    expect(body?.query).toBe('detect');
    expect(body?.kind).toBe('CLAIMS');
    expect(body?.sort).toBe('TITLE');
    expect(screen.getByLabelText('检索')).toHaveValue('detect');
  });

  it('explains an unsupported filter instead of sending an unknown field', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper' }],
      // The server does not advertise library.query: the read-state filter must
      // not be offered as if it worked.
      capabilities: [{ code: 'import.upload', availability: 'AVAILABLE', reason: null }],
    });
    renderLibrary(server);
    const user = userEvent.setup();
    await screen.findByRole('button', { name: '更多筛选' });
    await user.click(screen.getByRole('button', { name: '更多筛选' }));

    expect(await screen.findByTestId('filter-capability')).toHaveTextContent('暂不可用');
    expect(screen.getByRole('button', { name: '未读' })).toBeDisabled();
  });

  it('states the scope and offers a way out when nothing matches', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Paper' }] });
    renderLibrary(server, '?q=nothing-matches');
    expect(await screen.findByTestId('empty-scope')).toHaveTextContent('清除筛选');
    expect(screen.getByRole('button', { name: '清除筛选' })).toBeInTheDocument();
  });
});
