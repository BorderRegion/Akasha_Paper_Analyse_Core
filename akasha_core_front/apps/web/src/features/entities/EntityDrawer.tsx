/**
 * EntityDrawer (docs/03 §S08, docs/06 §专题与实体).
 *
 * Identity comes from STORED records: canonical name, stored aliases, stored
 * relations and the papers that reference them. The UI never infers a person's
 * identity, affiliation or ability from their name; an absent description shows
 * its missing reason instead of a guess.
 */

import type { ReactElement } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { MISSING_LABELS } from '../../lib/format';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import styles from './entities.module.css';

export interface EntityDrawerProps {
  entityId: string | null;
  onClose(): void;
}

export function EntityDrawer({ entityId, onClose }: EntityDrawerProps): ReactElement | null {
  const { api } = useSession();
  const card = useQuery({
    queryKey: ['ui', 'entity', entityId] as const,
    enabled: Boolean(entityId),
    queryFn: async () => (await api.entity(entityId as string)).data,
  });

  if (!entityId) return null;

  return (
    <aside className={styles.drawer} aria-label="实体详情" data-testid="entity-drawer">
      <header className={styles.header}>
        <h2>{card.data?.name ?? '载入中…'}</h2>
        <button type="button" onClick={onClose} aria-label="关闭实体详情">
          ×
        </button>
      </header>
      <AsyncBoundary
        state={card.isPending ? 'loading' : card.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(card.error)}
        onRetry={() => void card.refetch()}
      >
        {card.data ? (
          <div className={styles.body}>
            <p className="muted">
              类型 {card.data.entity_type} · 实体 {card.data.entity_id}
            </p>
            <h3>说明</h3>
            <p>
              {card.data.description.value ??
                `未报告（${MISSING_LABELS[card.data.description.missing_reason ?? 'UNKNOWN'] ?? '未知'}）`}
            </p>
            <h3>别名（服务器记录）</h3>
            {card.data.aliases.length ? (
              <ul>
                {card.data.aliases.map((alias) => (
                  <li key={alias}>{alias}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">没有已记录的别名。</p>
            )}
            <h3>关系</h3>
            {card.data.relations?.length ? (
              <ul>
                {card.data.relations.map((relation) => (
                  <li key={`${relation.relation_type}-${relation.other_entity_id}`}>
                    {relation.direction === 'OUT' ? '→' : '←'} {relation.relation_type}{' '}
                    {relation.other_entity_id}
                    {relation.confidence === null ? '（置信度未报告）' : ''}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">没有已存关系。</p>
            )}
            <h3>来源论文</h3>
            <ul data-testid="entity-sources">
              {card.data.source_refs.map((ref) => (
                <li key={ref.paper_id}>{ref.paper_id}</li>
              ))}
              {!card.data.source_refs.length ? <li className="muted">没有可核对来源</li> : null}
            </ul>
            <p className="muted">
              不按姓名推断能力或可靠性；只显示可核对的身份、机构与来源。
            </p>
          </div>
        ) : null}
      </AsyncBoundary>
    </aside>
  );
}
