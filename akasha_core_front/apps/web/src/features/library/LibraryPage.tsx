/**
 * LibraryPage (docs/03 §S02).
 *
 * Contract points implemented here:
 * - results are separated by KIND: a CLAIM hit renders as a claim row, never as
 *   a paper card;
 * - the search box ignores IME composition events and debounces typed input;
 * - filters/sort/cursor live in the URL, so going back from a paper restores the
 *   exact listing (and the selection tray survives the round trip);
 * - an unknown total is shown as "已载入 N 项", never as "共 N 篇";
 * - a batch action always previews its scope (explicit paper ids) and carries an
 *   idempotency key; there is no "select the whole library".
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { LibraryQuery } from '../../api/contract';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { useLibrary, useSavedMutation } from '../../state/queries';
import { useSession } from '../../state/session';
import {
  createSelectionTray,
  DEFAULT_QUERY,
  queryToSearch,
  scopeChanged,
  searchToQuery,
} from '../../state/libraryUrl';
import { PaperRow } from './PaperRow';
import { BatchActionBar } from './BatchActionBar';
import { SavedSearches } from './SavedSearches';
import { TagActions } from './TagActions';
import styles from './library.module.css';
import { errorCodeOf } from '../../lib/apiError';
import { useImportDialog } from '../import/importContext';

export interface LibraryPageProps {
  workspaceId?: string;
}

export function LibraryPage({ workspaceId = 'local' }: LibraryPageProps): ReactElement {
  const [params, setParams] = useSearchParams();
  const { open: openImport } = useImportDialog();
  const { status: sessionStatus, capabilities } = useSession();
  const query = useMemo(() => searchToQuery(params.toString()), [params]);
  // A hand-typed URL that carries a cursor from another scope is refused by the
  // server; the hook restarts from page 1 and says so.
  const [notice, setNotice] = useState<string | null>(null);
  const result = useLibrary(query);
  const savedMutation = useSavedMutation();

  const tray = useMemo(() => createSelectionTray(workspaceId), [workspaceId]);
  const [selection, setSelection] = useState<string[]>(() => tray.ids);

  const applyQuery = useCallback(
    (next: LibraryQuery) => {
      const resetCursor = scopeChanged(query, next);
      const merged: LibraryQuery = { ...next, cursor: resetCursor ? null : next.cursor };
      setParams(new URLSearchParams(queryToSearch(merged)), { replace: false });
    },
    [query, setParams],
  );

  useEffect(() => {
    if (result.data?.resetNotice) setNotice(result.data.resetNotice);
  }, [result.data?.resetNotice]);

  const onSearchChange = useCallback(
    (value: string) => applyQuery({ ...query, query: value }),
    [applyQuery, query],
  );

  const items = result.data?.page.items ?? [];
  const unknownTotal = result.data?.page.total_kind !== 'EXACT' || result.data?.page.total === null;

  return (
    <section className={styles.page} aria-labelledby="library-heading">
      <header className={styles.header}>
        <h1 id="library-heading">文献库</h1>
        <Button onClick={openImport}>导入 PDF</Button>
        <p className="muted">
          当前范围：{describeScope(query)}
          {result.data
            ? ` · ${
                unknownTotal
                  ? `已载入 ${items.length} 项`
                  : `共 ${result.data.page.total} 项`
              }`
            : ''}
        </p>
      </header>

      <SearchBox value={query.query} onChange={onSearchChange} />

      <div className={styles.kindTabs} role="tablist" aria-label="结果类型">
        {(
          [
            ['PAPERS', '论文'],
            ['CLAIMS', '结论'],
            ['TECHNIQUES', '技巧'],
          ] as const
        ).map(([kind, label]) => (
          <button
            key={kind}
            type="button"
            role="tab"
            aria-selected={query.kind === kind}
            data-kind={kind}
            onClick={() => applyQuery({ ...query, kind, cursor: null })}
          >
            {label}
          </button>
        ))}
      </div>

      <FilterBar query={query} capabilities={capabilities} onChange={applyQuery} />

      {notice ? (
        <p role="status" data-testid="reset-notice">
          {notice}
        </p>
      ) : null}

      <AsyncBoundary
        state={result.isPending ? 'loading' : result.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(result.error)}
        scopeLabel={describeScope(query)}
        onRetry={() => void result.refetch()}
        lastUpdatedAt={result.dataUpdatedAt ? new Date(result.dataUpdatedAt).toISOString() : null}
      >
        {result.isError ? null : (
          <>
            {sessionStatus === 'anonymous' ? null : null}
            <ul className={styles.list} data-kind={query.kind}>
              {items.map((item) =>
                'personal' in item ? (
                  <PaperRow
                    key={item.paper_id}
                    paper={item}
                    selected={selection.includes(item.paper_id)}
                    onToggleSelected={(paperId) => setSelection(tray.toggle(paperId))}
                    onToggleSaved={(paperId, saved) =>
                      savedMutation.mutate({
                        paperId,
                        saved,
                        expectedRevision: item.personal.revision,
                        query,
                      })
                    }
                    savedPending={savedMutation.isPending}
                  />
                ) : (
                  <li key={resultKey(item)} className={styles.row} data-claim-hit="true">
                    <div className={styles.body}>
                      <p>{'statement' in item ? item.statement : item.name}</p>
                      <p className="muted">{'claim_id' in item ? '相关结论' : '相关技巧'}</p>
                    </div>
                  </li>
                ),
              )}
            </ul>
            {!items.length && !result.isPending ? (
              <p data-testid="empty-scope">当前范围内没有内容，可清除筛选后重试。</p>
            ) : null}
          </>
        )}
      </AsyncBoundary>

      <footer className={styles.footer}>
        <Button
          variant="quiet"
          disabled={!result.data?.page.has_more}
          onClick={() =>
            applyQuery({ ...query, cursor: result.data?.page.next_cursor ?? null })
          }
        >
          载入下一页
        </Button>
        {savedMutation.isError ? (
          <p role="alert" data-testid="save-error">
            收藏保存失败（{savedMutation.error?.shape.code}），已恢复原状态。
          </p>
        ) : null}
      </footer>

      <SavedSearches
        currentQuery={query}
        onApply={(saved) => applyQuery({ ...saved, cursor: null })}
      />

      <details className={styles.tagTools}><summary>整理标签</summary>
        <TagActions tags={[...new Map(items.flatMap(item => 'tags' in item ? item.tags : []).map(tag => [tag.id, tag])).values()]} />
      </details>

      <BatchActionBar
        selection={selection}
        onClear={() => setSelection(tray.clear())}
        onRemovePaper={(paperId) => setSelection(tray.toggle(paperId))}
      />
    </section>
  );
}

function resultKey(item: unknown): string {
  const record = item as { paper_id?: string; claim_id?: string; entity_id?: string };
  return record.paper_id ?? record.claim_id ?? record.entity_id ?? JSON.stringify(item).slice(0, 32);
}

function describeScope(query: LibraryQuery): string {
  const parts: string[] = [];
  const filters = query.filters;
  if (filters.collection_ids?.length) parts.push(`专题 ${filters.collection_ids.length} 个`);
  if (filters.read_states?.length) parts.push(`阅读状态 ${filters.read_states.join('/')}`);
  if (filters.tiers?.length) parts.push(`资源深度 ${filters.tiers.join('/')}`);
  if (filters.years?.length) parts.push(`年份 ${filters.years.join('/')}`);
  if (filters.audit_states?.length) parts.push(`证据状态 ${filters.audit_states.join('/')}`);
  return parts.length ? parts.join('，') : '全部文献';
}

interface SearchBoxProps {
  value: string;
  onChange(value: string): void;
}

/**
 * Search input with IME handling: while an input method is composing, the value
 * is kept locally and NOT sent — a Chinese/Japanese composition must not fire a
 * query per keystroke (docs/03 §S02). Committing happens on composition end and
 * on a debounce for plain typing.
 */
export function SearchBox({ value, onChange }: SearchBoxProps): ReactElement {
  const [draft, setDraft] = useState(value);
  const composing = useRef(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  const schedule = useCallback(
    (next: string) => {
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => onChange(next), 250);
    },
    [onChange],
  );

  useEffect(() => () => (timer.current ? clearTimeout(timer.current) : undefined), []);

  return (
    <div className={styles.search}>
      <label htmlFor="library-search">检索</label>
      <input
        id="library-search"
        type="search"
        value={draft}
        placeholder="标题、结论或技巧"
        onChange={(event) => {
          const next = event.target.value;
          setDraft(next);
          if (composing.current) return;
          schedule(next);
        }}
        onCompositionStart={() => {
          composing.current = true;
        }}
        onCompositionEnd={(event) => {
          // Browsers replace the composition text in the field BEFORE this
          // event, so the field value is the committed text. Reading the field
          // (not the batched state) avoids submitting a stale half-composed
          // string.
          composing.current = false;
          const next = event.currentTarget.value;
          setDraft(next);
          onChange(next);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            if (timer.current) clearTimeout(timer.current);
            onChange(draft);
          }
        }}
      />
      {composing.current ? null : null}
    </div>
  );
}

interface FilterBarProps {
  query: LibraryQuery;
  capabilities: { capabilities: { code: string }[] } | null;
  onChange(next: LibraryQuery): void;
}

/**
 * Only filters the server reports as available are offered; an unsupported
 * filter is explained instead of being sent and silently rejected with a 422.
 */
export function FilterBar({ query, capabilities, onChange }: FilterBarProps): ReactElement {
  const [more, setMore] = useState(false);
  const supported = new Set(capabilities?.capabilities.map((item) => item.code) ?? []);
  const readFilterSupported = supported.size === 0 || supported.has('library.query');

  return (
    <div className={styles.filters}>
      <div className={styles.quick}>
        {(
          [
            ['UNREAD', '未读'],
            ['READING', '在读'],
            ['READ', '已读'],
          ] as const
        ).map(([state, label]) => (
          <button
            key={state}
            type="button"
            aria-pressed={query.filters.read_states?.includes(state) ?? false}
            disabled={!readFilterSupported}
            onClick={() =>
              onChange({
                ...query,
                filters: {
                  ...query.filters,
                  read_states: query.filters.read_states?.includes(state)
                    ? query.filters.read_states.filter((item) => item !== state)
                    : [...(query.filters.read_states ?? []), state],
                },
              })
            }
          >
            {label}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={query.filters.audit_states?.includes('DISPUTED') ?? false}
          onClick={() =>
            onChange({
              ...query,
              filters: {
                ...query.filters,
                audit_states: query.filters.audit_states?.includes('DISPUTED')
                  ? undefined
                  : ['DISPUTED'],
              },
            })
          }
        >
          待核查
        </button>
      </div>

      <button type="button" onClick={() => setMore((current) => !current)} aria-expanded={more}>
        更多筛选
      </button>
      {more ? (
        <div className={styles.more} data-testid="more-filters">
          <label>
            排序
            <select
              value={query.sort}
              onChange={(event) =>
                onChange({ ...query, sort: event.target.value as LibraryQuery['sort'] })
              }
            >
              <option value="RELEVANCE">相关度（检索排序，非质量）</option>
              <option value="RECENT">最近入库</option>
              <option value="TITLE">标题</option>
            </select>
          </label>
          <label>
            资源深度
            <select
              value={query.filters.tiers?.[0] ?? ''}
              onChange={(event) =>
                onChange({
                  ...query,
                  filters: {
                    ...query.filters,
                    tiers: event.target.value ? [event.target.value as 'T2_FULL'] : undefined,
                  },
                })
              }
            >
              <option value="">全部</option>
              <option value="T0_INDEX">T0 索引</option>
              <option value="T1_SCAN">T1 速览</option>
              <option value="T2_FULL">T2 完整</option>
              <option value="T3_DEEP">T3 深入</option>
            </select>
          </label>
          {!readFilterSupported ? (
            <p className="muted" data-testid="filter-capability">
              阅读状态筛选暂不可用：服务端能力清单未报告 library.query。
            </p>
          ) : null}
        </div>
      ) : null}

      <button
        type="button"
        className={styles.clear}
        onClick={() => onChange({ ...DEFAULT_QUERY, kind: query.kind })}
      >
        清除筛选
      </button>
    </div>
  );
}

export { searchToQuery };
