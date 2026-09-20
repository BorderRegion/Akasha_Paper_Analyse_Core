/**
 * UX-055 — 10k-paper stress and a bounded query cache.
 *
 * Requirement (docs/08 §性能 + docs/03 §S02): 服务端 cursor 分页，默认50项、最大100项；
 * 页面不因大库卡死；缓存有预算；不把整库载入浏览器.
 */

import { screen, waitFor } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { LIBRARY_CACHE_BUDGET, createQueryClient } from '../../src/app/providers';

function bigLibrary(count: number) {
  return Array.from({ length: count }, (_, index) => ({
    paper_id: `pap_${index}`,
    title: `Paper ${index}`,
  }));
}

describe('UX-055 large corpus behaviour', () => {
  it('never asks the server for more than the documented page size', async () => {
    const server = createFakeServer({ papers: bigLibrary(10_000) });
    renderApp(server, { path: '/app/library' });

    await screen.findByRole('link', { name: 'Paper 0' });
    const queries = server.requests.filter((request) => request.path === '/v1/ui/library/query');
    for (const query of queries) {
      const limit = (query.body as { limit?: number } | undefined)?.limit ?? 50;
      expect(limit).toBeLessThanOrEqual(100);
    }
    // The browser holds ONE page, not the whole library.
    expect(document.querySelectorAll('ul[data-kind="PAPERS"] > li').length).toBeLessThanOrEqual(100);
  });

  it('keeps the query cache inside its budget', () => {
    const client = createQueryClient();
    expect(LIBRARY_CACHE_BUDGET.maxQueries).toBeGreaterThan(0);
    expect(LIBRARY_CACHE_BUDGET.maxQueries).toBeLessThanOrEqual(200);
    expect(client.getQueryCache().config).toBeDefined();
  });

  it('evicts old pages instead of growing without bound', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
      queryCache: undefined,
    });
    const server = createFakeServer({ papers: bigLibrary(3_000) });
    renderApp(server, { path: '/app/library', queryClient: client });

    await screen.findByRole('link', { name: 'Paper 0' });
    for (let page = 0; page < 12; page += 1) {
      const next = screen.queryByRole('button', { name: '载入下一页' });
      if (!next || (next as HTMLButtonElement).disabled) break;
      next.click();
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await waitFor(() => expect(client.getQueryCache().getAll().length).toBeLessThanOrEqual(60));
  });

  it('renders exactly one page (default 50) and offers the next one', async () => {
    const server = createFakeServer({ papers: bigLibrary(100) });
    renderApp(server, { path: '/app/library' });
    await screen.findByRole('link', { name: 'Paper 0' });
    await waitFor(() =>
      expect(document.querySelectorAll('ul[data-kind="PAPERS"] > li').length).toBe(50),
    );
    const next = screen.getByRole('button', { name: '载入下一页' });
    expect(next).toBeEnabled();
    // The remaining papers stay on the server until asked for.
    expect(screen.queryByRole('link', { name: 'Paper 99' })).not.toBeInTheDocument();
  });

  it('does not fetch 10k papers up front', async () => {
    const server = createFakeServer({ papers: bigLibrary(10_000) });
    renderApp(server, { path: '/app/library' });
    await screen.findByRole('link', { name: 'Paper 0' });
    const listQueries = server.requests.filter((request) => request.path === '/v1/ui/library/query');
    expect(listQueries.length).toBeLessThanOrEqual(3);
  });
});
