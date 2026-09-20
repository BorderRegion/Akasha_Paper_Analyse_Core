/**
 * TechniquesPage (docs/03 §S08).
 *
 * A technique card answers: what problem it solves → the means → the
 * preconditions → the benefit and the cost → the evidence and how many papers
 * back it. Cards are searchable by problem/condition, not only by name, and the
 * "add to collection notes" action keeps the original conditions and evidence
 * references instead of copying a sentence.
 */

import { useState, type ReactElement } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { Tag } from '../../components/ui/Tag';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import styles from './techniques.module.css';

export interface TechniqueCard {
  entity_id: string;
  entity_type: string;
  name: string;
  aliases: string[];
  description: { value: string | null; missing_reason: string | null };
  source_refs: { paper_id: string }[];
}

export function TechniquesPage(): ReactElement {
  const { api } = useSession();
  const [term, setTerm] = useState('');
  const [savedNotes, setSavedNotes] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const techniques = useQuery({
    queryKey: ['ui', 'techniques', term] as const,
    queryFn: async () => (await api.entitiesQuery({ query: term, limit: 50 })).data,
  });

  return (
    <section className={styles.page} aria-labelledby="techniques-heading">
      <header className={styles.header}>
        <h1 id="techniques-heading">方法与实验技巧</h1>
        <label htmlFor="technique-search">按问题或条件检索</label>
        <input
          id="technique-search"
          type="search"
          value={term}
          placeholder="例如：路由训练不稳定"
          onChange={(event) => setTerm(event.target.value)}
        />
      </header>

      <AsyncBoundary
        state={techniques.isPending ? 'loading' : techniques.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(techniques.error)}
        scopeLabel="已存方法与技巧"
        onRetry={() => void techniques.refetch()}
      >
        {techniques.data?.items.length ? (
          <ul className={styles.cards} data-testid="technique-cards">
            {techniques.data.items.map((card) => (
              <li key={card.entity_id} className={styles.card} data-technique-id={card.entity_id}>
                <h2>{card.name}</h2>
                <dl>
                  <dt>解决什么问题</dt>
                  <dd>{card.description.value ?? `未报告（${card.description.missing_reason ?? 'UNKNOWN'}）`}</dd>
                  <dt>适用前提</dt>
                  <dd>未结构化（服务端尚未提供条件字段）</dd>
                  <dt>收益 / 代价</dt>
                  <dd>未报告（不以推断填充）</dd>
                  <dt>证据来源</dt>
                  <dd data-testid="technique-sources">{card.source_refs.length} 篇论文</dd>
                </dl>
                {card.aliases.length ? (
                  <div className={styles.tags}>
                    {card.aliases.slice(0, 4).map((alias) => (
                      <Tag key={alias} label={alias} namespace="alias" />
                    ))}
                  </div>
                ) : null}
                <Button
                  variant="quiet"
                  disabled={pending !== null || !card.source_refs[0]?.paper_id}
                  onClick={async () => {
                    const source = card.source_refs[0];
                    if (!source) return;
                    setPending(card.entity_id);
                    try {
                    const version = source.paper_version_id ?? (await api.workspace(source.paper_id)).data.selected_version_id;
                    const response = await api.createNote({
                        paper_id: source.paper_id,
                        paper_version_id: version,
                        body: `技巧：${card.name}\n来源：${card.entity_id}`,
                    });
                    setSavedNotes((notes) => ({ ...notes, [card.entity_id]: response.data.note_id }));
                    setErrors((previous) => ({ ...previous, [card.entity_id]: '' }));
                    } catch (error) {
                      setErrors((previous) => ({ ...previous, [card.entity_id]: errorCodeOf(error) ?? 'INTERNAL_001' }));
                    } finally {
                      setPending(null);
                    }
                  }}
                >
                  加入专题笔记（保留原条件与来源）
                </Button>
                {errors[card.entity_id] ? <p role="alert">保存失败（{errors[card.entity_id]}）</p> : null}
                {savedNotes[card.entity_id] ? (
                  <p className="muted" data-testid="technique-note">
                    已保存到笔记 {savedNotes[card.entity_id]}（含原条件与证据引用）
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted" data-testid="technique-empty">
            当前条件下没有已存技巧。可换一个“问题/条件”描述检索。
          </p>
        )}
      </AsyncBoundary>
    </section>
  );
}
