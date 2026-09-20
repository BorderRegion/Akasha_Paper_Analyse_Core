/**
 * CollectionWorkspacePage (docs/03 §S07).
 *
 * A collection snapshot ALWAYS carries its scope: collection id, selection hash,
 * sample size (versions, claims, year range). A conclusion drawn from a subset is
 * labelled as such — it is never written as a domain-wide trend.
 *
 * Analysis is never started by opening the page: the stored results are shown, and
 * generating new intelligence is an explicit user action (docs/03 §S07).
 */

import { useState, type ReactElement } from 'react';
import { useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { errorCodeOf } from '../../lib/apiError';
import { claimTypeLabel } from '../../lib/status';
import { useSession } from '../../state/session';
import styles from './collections.module.css';

export function CollectionWorkspacePage(): ReactElement {
  const { collectionId = '' } = useParams();
  const { api } = useSession();
  const queryClient = useQueryClient();
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);
  const [generateResult, setGenerateResult] = useState<string | null>(null);

  const workspace = useQuery({
    queryKey: ['ui', 'collection', collectionId] as const,
    enabled: Boolean(collectionId),
    queryFn: async () => (await api.collectionWorkspace(collectionId)).data,
  });

  if (workspace.isPending) {
    return (
      <section className={styles.page}>
        <AsyncBoundary state="loading" />
      </section>
    );
  }
  if (workspace.isError || !workspace.data) {
    return (
      <section className={styles.page}>
        <AsyncBoundary
          state="error"
          errorCode={errorCodeOf(workspace.error)}
          onRetry={() => void workspace.refetch()}
        />
      </section>
    );
  }

  const data = workspace.data;
  const years = data.sample.years;

  return (
    <section className={styles.page} aria-labelledby="collection-heading">
      <header className={styles.header}>
        <h1 id="collection-heading">{data.title}</h1>
        <p className="muted" data-testid="collection-scope">
          范围：专题 {data.collection_id} · selection_hash {data.selection_hash} · 样本{' '}
          {data.selected_paper_count} 篇（已分析 {data.analyzed_paper_count} 篇）·
          {data.sample.paper_versions} 个版本 / {data.sample.claims} 条结论
          {years ? ` · 年份 ${years[0]}–${years[1]}` : ' · 年份未报告'}
        </p>
        <p className="muted" data-testid="subset-notice">
          以下结论仅代表本专题的样本范围，不能写成全领域趋势。
        </p>
      </header>

      <div className={styles.actions}>
        <Button
          disabled={generating}
          onClick={async () => {
            setGenerating(true);
            setGenerateError(null);
            try {
              const response = await api.submitOperation({
                kind: 'corpus_refresh',
                payload: {
                  collection_id: data.collection_id,
                  selection_hash: data.selection_hash,
                },
                idempotency_key: `corpus_refresh:${data.collection_id}:${data.selection_hash}`,
              });
              setGenerateResult(response.data.operation_id);
              void queryClient.invalidateQueries({ queryKey: ['ui', 'collection', collectionId] });
            } catch (error) {
              setGenerateError(errorCodeOf(error));
            } finally {
              setGenerating(false);
            }
          }}
        >
          {generating ? '生成中…' : '生成专题观察（显式触发，不会自动调用模型）'}
        </Button>
        {generateResult ? (
          <span role="status" data-testid="generate-result">
            已受理 operation {generateResult}；完成后才会刷新结论。
          </span>
        ) : null}
        {generateError ? (
          <span role="alert">生成失败（{generateError}），未改动已存结论。</span>
        ) : null}
      </div>

      <section className={styles.block} aria-labelledby="insights-heading">
        <h2 id="insights-heading">目前已知 / 存在争议</h2>
        {data.insights.length ? (
          <ul className={styles.insights}>
            {data.insights.slice(0, 6).map((insight) => (
              <li key={insight.statement} data-testid="insight">
                <span className={styles.insightTitle}>{insight.title}</span>
                <span className="muted">{claimTypeLabel(insight.claim_type)}</span>
                <p>{insight.statement}</p>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted" data-testid="no-insights">
            该专题样本内没有已存结论可以概括（未提取，不是“没有问题”）。
          </p>
        )}
      </section>
    </section>
  );
}
