/**
 * CollectionsPage — the collection list with its own scope facts (docs/03 §S07).
 * A collection list is a scope selector, not a dashboard: no knowledge graph on
 * the landing surface.
 */

import { useState, type ReactElement } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import styles from './collections.module.css';
import { Button } from '../../components/ui/Button';

export function CollectionsPage(): ReactElement {
  const { client, api } = useSession();
  const [params] = useSearchParams();
  const paperId = params.get('paper_id');
  const [name, setName] = useState('');
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mutate = async (action: () => Promise<unknown>, success: string) => {
    setPending(true);
    setError(null);
    try { await action(); setMessage(success); await collections.refetch(); }
    catch (failure) { setError(errorCodeOf(failure) ?? 'INTERNAL_001'); }
    finally { setPending(false); }
  };
  const collections = useQuery({
    queryKey: ['core', 'collections'] as const,
    queryFn: async () => {
      const response = await client.request<{ collections?: Array<{ collection_id: string; name: string }> } | Array<{ collection_id: string; name: string }>>(
        '/v1/ui/collections',
      );
      const data = response.data;
      return Array.isArray(data) ? data : (data.collections ?? []);
    },
  });

  return (
    <section className={styles.page} aria-labelledby="collections-heading">
      <h1 id="collections-heading">研究专题</h1>
      <p className="muted">把有关的论文放在一起。一个问题，可以慢慢想。</p>
      <form onSubmit={event => {
        event.preventDefault();
        void mutate(() => api.createCollection(name), '专题已创建');
      }}>
        <label>专题名称 <input value={name} onChange={event => setName(event.target.value)} maxLength={255} /></label>
        <Button type="submit" disabled={pending || !name.trim()}>创建专题</Button>
      </form>
      {paperId ? <p>正在管理当前论文的专题归属。移出专题不会删除论文或笔记。</p> : null}
      {message ? <p role="status">{message}</p> : null}
      {error ? <p role="alert">操作失败（{error}），请重试。</p> : null}
      <AsyncBoundary
        state={collections.isPending ? 'loading' : collections.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(collections.error)}
        scopeLabel="全部专题"
        onRetry={() => void collections.refetch()}
      >
        {collections.data?.length ? (
          <ul className={styles.list}>
            {collections.data.map((collection) => (
              <li key={collection.collection_id}>
                <Link to={`/app/collections/${collection.collection_id}`}>{collection.name}</Link>
                {paperId ? <>
                  <Button disabled={pending} onClick={() => void mutate(
                    () => api.collectionPaper(collection.collection_id, paperId), '论文已加入专题',
                  )}>加入此专题</Button>
                  <Button disabled={pending} onClick={() => void mutate(
                    () => api.collectionPaper(collection.collection_id, paperId, true), '论文已移出专题',
                  )}>移出此专题</Button>
                </> : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted">还没有专题。给正在关心的问题起个名字，再放入相关论文吧。</p>
        )}
      </AsyncBoundary>
    </section>
  );
}
