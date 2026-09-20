/**
 * The library URL is the source of truth for filters (spec docs/03 §S02:
 * "搜索条件和结果 scope 保存至 URL"), so returning from a paper restores the
 * exact query, and a shared link means the same thing on both ends.
 *
 * Selection is separate from the query: a selection may span pages and is kept
 * in a per-workspace tray (sessionStorage) while the filters stay in the URL.
 * "Select the whole library" is deliberately NOT offered — a batch action always
 * names explicit paper ids (docs/03 §S02).
 */

import type { LibraryQuery, ReadState, SupportState, Tier } from '../api/contract';

export const DEFAULT_QUERY: LibraryQuery = {
  query: '',
  kind: 'PAPERS',
  filters: {},
  sort: 'RELEVANCE',
  cursor: null,
  limit: 50,
};

const READ_STATES: ReadState[] = ['UNREAD', 'READING', 'READ', 'LATER'];
const TIERS: Tier[] = ['T0_INDEX', 'T1_SCAN', 'T2_FULL', 'T3_DEEP'];
const SUPPORT_STATES: SupportState[] = [
  'UNVERIFIED',
  'SUPPORTED',
  'PARTIALLY_SUPPORTED',
  'DISPUTED',
  'UNSUPPORTED',
  'RETRACTED',
  'INSUFFICIENT_EVIDENCE',
];

function list(value: string | null, allowed?: readonly string[]): string[] | undefined {
  if (!value) return undefined;
  const parts = value
    .split(',')
    .map((part) => part.trim())
    .filter((part) => part.length > 0 && (!allowed || allowed.includes(part)));
  return parts.length ? parts : undefined;
}

/** Query → URL search params (cursor is included so a reload keeps the page). */
export function queryToSearch(query: LibraryQuery): string {
  const params = new URLSearchParams();
  if (query.query) params.set('q', query.query);
  if (query.kind !== 'PAPERS') params.set('kind', query.kind);
  if (query.sort !== 'RELEVANCE') params.set('sort', query.sort);
  if (query.limit !== 50) params.set('limit', String(query.limit));
  if (query.cursor) params.set('cursor', query.cursor);
  const filters = query.filters;
  if (filters.collection_ids?.length) params.set('collection', filters.collection_ids.join(','));
  if (filters.tag_ids?.length) params.set('tag', filters.tag_ids.join(','));
  if (filters.read_states?.length) params.set('read', filters.read_states.join(','));
  if (filters.years?.length) params.set('year', filters.years.join(','));
  if (filters.venue_ids?.length) params.set('venue', filters.venue_ids.join(','));
  if (filters.author_ids?.length) params.set('author', filters.author_ids.join(','));
  if (filters.tiers?.length) params.set('tier', filters.tiers.join(','));
  if (filters.audit_states?.length) params.set('audit', filters.audit_states.join(','));
  return params.toString();
}

export function searchToQuery(search: string): LibraryQuery {
  const params = new URLSearchParams(search);
  const limitRaw = Number(params.get('limit'));
  const years = params.get('year')
    ? params
        .get('year')!
        .split(',')
        .map((part) => Number(part.trim()))
        .filter((value) => Number.isInteger(value))
    : undefined;
  const kind = params.get('kind');
  const sort = params.get('sort');
  return {
    query: params.get('q') ?? '',
    kind: kind === 'CLAIMS' || kind === 'TECHNIQUES' ? kind : 'PAPERS',
    filters: {
      collection_ids: list(params.get('collection')),
      tag_ids: list(params.get('tag')),
      read_states: list(params.get('read'), READ_STATES) as ReadState[] | undefined,
      years: years?.length ? years : undefined,
      venue_ids: list(params.get('venue')),
      author_ids: list(params.get('author')),
      tiers: list(params.get('tier'), TIERS) as Tier[] | undefined,
      audit_states: list(params.get('audit'), SUPPORT_STATES) as SupportState[] | undefined,
    },
    sort: sort === 'RECENT' || sort === 'TITLE' ? sort : 'RELEVANCE',
    cursor: params.get('cursor'),
    limit: Number.isInteger(limitRaw) && limitRaw >= 1 && limitRaw <= 100 ? limitRaw : 50,
  };
}

/** True when the user changed the scope: the cursor must be dropped. */
export function scopeChanged(previous: LibraryQuery, next: LibraryQuery): boolean {
  return (
    previous.query !== next.query ||
    previous.kind !== next.kind ||
    previous.sort !== next.sort ||
    JSON.stringify(previous.filters) !== JSON.stringify(next.filters)
  );
}

export interface SelectionTray {
  ids: string[];
  toggle(paperId: string): string[];
  clear(): string[];
  has(paperId: string): boolean;
}

const SELECTION_PREFIX = 'paperintel.selection';

export function selectionKey(workspaceId: string): string {
  return `${SELECTION_PREFIX}.${workspaceId}`;
}

/**
 * A selection tray backed by sessionStorage, scoped by workspace AND origin so
 * two deployments on the same browser cannot mix their ids (docs/05).
 */
export function createSelectionTray(
  workspaceId: string,
  storage: Storage | null = typeof window === 'undefined' ? null : window.sessionStorage,
): SelectionTray {
  const key = selectionKey(workspaceId);
  let ids: string[] = [];
  if (storage) {
    try {
      const raw = storage.getItem(key);
      const parsed = raw ? (JSON.parse(raw) as unknown) : [];
      if (Array.isArray(parsed)) ids = parsed.filter((value): value is string => typeof value === 'string');
    } catch {
      ids = [];
    }
  }
  const persist = () => {
    if (!storage) return;
    try {
      storage.setItem(key, JSON.stringify(ids));
    } catch {
      /* a full or blocked storage must not break selection in memory */
    }
  };
  return {
    get ids() {
      return [...ids];
    },
    has: (paperId) => ids.includes(paperId),
    toggle(paperId) {
      ids = ids.includes(paperId) ? ids.filter((id) => id !== paperId) : [...ids, paperId];
      persist();
      return [...ids];
    },
    clear() {
      ids = [];
      persist();
      return [];
    },
  };
}
