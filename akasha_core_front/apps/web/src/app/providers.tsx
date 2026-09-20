import type { ReactElement, ReactNode } from 'react';
import { createElement } from 'react';
import { QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ApiClient } from '../api/client';
import { SessionProvider } from '../state/session';
import { PdfEngineProvider, type PdfEngineFactory } from '../reader/engineContext';

/**
 * Cache budget (docs/08 §性能): the browser holds a bounded number of server
 * pages. A 10k-paper library is paged on the SERVER; the client never keeps the
 * whole corpus, and old pages are evicted instead of growing without bound.
 */
export const LIBRARY_CACHE_BUDGET = {
  /** Pages kept in memory at once. */
  maxQueries: 60,
  /** A page older than this is dropped rather than kept "just in case". */
  gcTimeMs: 5 * 60 * 1000,
} as const;

/**
 * One QueryClient per app instance: a browser tab keeps its cache, tests get a
 * fresh one (and a logout clears it).
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The shared client already retries reads with backoff; React Query must
        // not add a second, invisible retry layer on top of it.
        retry: false,
        refetchOnWindowFocus: false,
        staleTime: 15_000,
        gcTime: LIBRARY_CACHE_BUDGET.gcTimeMs,
      },
      mutations: { retry: false },
    },
    queryCache: new QueryCache({
      onSuccess: () => {
        // Enforce the budget: the oldest cached page is evicted when the number
        // of entries exceeds it (the client is the only place that knows).
        const cache = queryClientRef;
        if (!cache) return;
        const entries = cache.getQueryCache().getAll();
        if (entries.length <= LIBRARY_CACHE_BUDGET.maxQueries) return;
        const excess = entries
          .sort((a, b) => (a.state.dataUpdatedAt ?? 0) - (b.state.dataUpdatedAt ?? 0))
          .slice(0, entries.length - LIBRARY_CACHE_BUDGET.maxQueries);
        for (const entry of excess) cache.removeQueries({ queryKey: entry.queryKey, exact: true });
      },
    }),
  });
}

/** The most recently created client (used by the cache-budget eviction hook). */
let queryClientRef: QueryClient | null = null;

export interface ProvidersProps {
  client: ApiClient;
  queryClient?: QueryClient;
  /** Test seam: a deterministic PDF engine (jsdom has no canvas). */
  pdfEngineFactory?: PdfEngineFactory;
  children: ReactNode;
}

export function Providers({
  client,
  queryClient,
  pdfEngineFactory,
  children,
}: ProvidersProps): ReactElement {
  const resolved = queryClient ?? createQueryClient();
  queryClientRef = resolved;
  return createElement(
    QueryClientProvider,
    { client: resolved },
    createElement(
      SessionProvider,
      { client, children: createElement(PdfEngineProvider, { factory: pdfEngineFactory, children }) },
    ),
  );
}
