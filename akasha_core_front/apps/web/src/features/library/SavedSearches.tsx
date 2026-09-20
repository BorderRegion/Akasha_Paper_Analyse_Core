/**
 * SavedSearches (docs/06 §专题与实体 + UX-044).
 *
 * A saved search stores the FULL filter structure with a schema version and its
 * revision — never a transient cursor. Renaming or re-scoping sends the revision
 * it read: a lost race is a conflict that keeps BOTH copies so the user can
 * recover, instead of silently overwriting someone else's edit.
 */

import { useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '../../components/ui/Button';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { ApiError } from '../../api/errors';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import type { LibraryQuery } from '../../api/contract';
import type { SavedSearch } from '../../api/ui';
import styles from './saved.module.css';

export interface SavedSearchesProps {
  currentQuery: LibraryQuery;
  onApply(query: LibraryQuery): void;
}

export function SavedSearches({ currentQuery, onApply }: SavedSearchesProps): ReactElement {
  const { api } = useSession();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [conflict, setConflict] = useState<{ local: string; server: SavedSearch | null } | null>(null);
  const [editing, setEditing] = useState<{ search: SavedSearch; draft: string } | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const list = useQuery({
    queryKey: ['ui', 'saved-searches'] as const,
    queryFn: async () => (await api.savedSearches()).data.items,
  });

  const create = useMutation({
    // A saved search stores the filter structure, never the transient cursor the
    // user happens to be on (docs/06 §专题与实体).
    mutationFn: async () =>
      (await api.createSavedSearch({ name: name.trim(), query: { ...currentQuery, cursor: null } })).data,
    onSuccess: () => {
      setName('');
      setErrorCode(null);
      void queryClient.invalidateQueries({ queryKey: ['ui', 'saved-searches'] });
    },
    onError: (error) => setErrorCode(errorCodeOf(error)),
  });

  const rename = useMutation({
    mutationFn: async (input: { search: SavedSearch; nextName: string }) =>
      (
        await api.patchSavedSearch(input.search.saved_search_id, {
          name: input.nextName,
          expected_revision: input.search.revision,
        })
      ).data,
    onSuccess: () => {
      setConflict(null);
      setEditing(null);
      setErrorCode(null);
      void queryClient.invalidateQueries({ queryKey: ['ui', 'saved-searches'] });
    },
    onError: async (error, variables) => {
      if (error instanceof ApiError && error.shape.code === 'REVISION_CONFLICT') {
        // Keep the local draft AND the server copy: nothing is discarded.
        let latest: SavedSearch | null = null;
        try { latest = (await api.savedSearches()).data.items.find(item => item.saved_search_id === variables.search.saved_search_id) ?? null; } catch { /* Keep the local draft; do not guess a revision. */ }
        setConflict({ local: variables.nextName, server: latest });
        setEditing(null);
        setErrorCode('REVISION_CONFLICT');
        return;
      }
      setErrorCode(errorCodeOf(error));
    },
  });

  return (
    <section className={styles.wrap} aria-labelledby="saved-heading" data-testid="saved-searches">
      <h2 id="saved-heading">保存的检索</h2>
      <div className={styles.create}>
        <label htmlFor="saved-name">名称</label>
        <input
          id="saved-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          placeholder="例如：待核查的目标检测"
        />
        <Button disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
          保存当前筛选
        </Button>
      </div>

      <AsyncBoundary state={list.isPending ? 'loading' : list.isError ? 'error' : 'ready'} errorCode={errorCodeOf(list.error)} onRetry={() => void list.refetch()}>
      {list.data ? <ul className={styles.list}>
        {(list.data ?? []).map((search) => (
          <li key={search.saved_search_id}>
            <button type="button" onClick={() => onApply(search.query)}>
              {search.name}
            </button>
            <Button
              variant="quiet"
              disabled={rename.isPending}
              onClick={() => { setConflict(null); setErrorCode(null); setEditing({ search, draft: search.name }); }}
            >
              改名
            </Button>
          </li>
        ))}
        {!list.data?.length ? <li className="muted">还没有保存的检索。</li> : null}
      </ul> : null}
      </AsyncBoundary>
      {editing ? <form className={styles.create} onSubmit={event => { event.preventDefault(); if (editing.draft.trim() && !rename.isPending) rename.mutate({ search: editing.search, nextName: editing.draft.trim() }); }}>
        <label htmlFor="rename-search">新名称</label>
        <input id="rename-search" value={editing.draft} disabled={rename.isPending} onChange={event => setEditing({ ...editing, draft: event.target.value })} />
        <Button type="submit" disabled={!editing.draft.trim() || rename.isPending}>保存名称</Button>
        <Button variant="quiet" disabled={rename.isPending} onClick={() => setEditing(null)}>取消改名</Button>
      </form> : null}

      {conflict ? (
        <div role="alert" data-testid="saved-conflict">
          <p>服务器上的名称已更新，两份内容都保留：</p>
          <ul>
            <li>
              本地草稿：<strong>{conflict.local}</strong>
            </li>
            <li>
              服务器版本：<strong>{conflict.server?.name ?? '暂时无法读取，请刷新后重试'}</strong>
            </li>
          </ul>
          <Button
            disabled={!conflict.server || rename.isPending}
            onClick={() =>
              conflict.server && rename.mutate({
                search: conflict.server,
                nextName: conflict.local,
              })
            }
          >
            用本地名称重试
          </Button>
          <Button variant="quiet" onClick={() => { setConflict(null); setErrorCode(null); void list.refetch(); }}>
            保留服务器版本
          </Button>
        </div>
      ) : null}
      {errorCode && !conflict ? (
        <p role="alert">保存失败（{errorCode}），未改动已保存的检索。</p>
      ) : null}
    </section>
  );
}
