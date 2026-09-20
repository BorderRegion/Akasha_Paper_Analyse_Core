/**
 * ComparePage (docs/03 §S09).
 *
 * Rows are fixed (problem/method/hypothesis/data/cost/evidence/limitation) and
 * differences are highlighted without heavy animation. A quantitative cell is
 * only enabled when the protocol keys agree; differing hardware/budget/protocol
 * cells are grouped and marked "不能直接比较" — the UI never computes a fake
 * improvement and never declares a winner. Missing values show their reason.
 */

import { useEffect, useState, type ReactElement } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { errorCodeOf } from '../../lib/apiError';
import { MISSING_LABELS } from '../../lib/format';
import { useSession } from '../../state/session';
import styles from './compare.module.css';

export const DIMENSION_LABELS: Record<string, string> = {
  problem: '问题',
  method: '方法',
  hypothesis: '假设',
  dataset: '数据',
  cost: '成本',
  evidence: '证据',
  limitation: '局限',
};

export function ComparePage(): ReactElement {
  const { api } = useSession();
  const [params, setParams] = useSearchParams();
  const versions = (params.get('versions') ?? '').split(',').filter(Boolean);
  const [selected, setSelected] = useState<string[]>(versions);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);

  useEffect(() => {
    setSelected(versions);
  }, [params]);

  const comparison = useQuery({
    queryKey: ['ui', 'compare', selected] as const,
    enabled: selected.length >= 2 && selected.length <= 4,
    queryFn: async () => (await api.compare(selected)).data,
  });

  const documents = comparison.data?.papers ?? {};
  const rows = Object.keys(DIMENSION_LABELS);

  return (
    <section className={styles.page} aria-labelledby="compare-heading">
      <header className={styles.header}>
        <h1 id="compare-heading">论文比较（2–4 篇）</h1>
        <p className="muted">
          当前选择 {selected.length} 篇。量化比较只在协议键一致时开启；不同硬件/预算/评估设置
          默认分组并标注“不能直接比较”。
        </p>
        {selected.length < 2 ? (
          <p role="status" data-testid="compare-needs-two">
            至少选择 2 篇才能比较；全库冠军式比较不提供。
          </p>
        ) : null}
        {selected.length > 4 ? <p role="alert">最多选择 4 篇，请减少选择后比较。</p> : null}
      </header>

      {selected.length >= 2 && selected.length <= 4 ? (
        <AsyncBoundary
          state={comparison.isPending ? 'loading' : comparison.isError ? 'error' : 'ready'}
          errorCode={errorCodeOf(comparison.error)}
          scopeLabel={`${selected.length} 个版本`}
          onRetry={() => void comparison.refetch()}
        >
          <div className={styles.toolbar}>
            <span className="muted" data-testid="selection-hash">
              selection_hash {comparison.data?.selection_hash ?? '—'} · analysis_revision{' '}
              {comparison.data?.analysis_revision ?? '—'}
            </span>
            <Button
              variant="quiet"
              onClick={async () => {
                setCopied(false);
                setCopyError(false);
                const header = ['dimension', ...selected];
                const lines = [
                  `selection_hash\t${comparison.data?.selection_hash ?? ''}`,
                  `analysis_revision\t${comparison.data?.analysis_revision ?? ''}`,
                  header.join('\t'),
                ];
                for (const dimension of rows) {
                  const cells = selected.map((versionId) =>
                    cellText(comparison.data?.cells, versionId, dimension),
                  );
                  lines.push([DIMENSION_LABELS[dimension], ...cells].join('\t'));
                }
                try { await navigator.clipboard.writeText(lines.join('\n')); setCopied(true); }
                catch { setCopyError(true); }
              }}
            >
              复制为表格（含版本/条件/单位）
            </Button>
            {copied ? <span className="muted">已复制（带 selection_hash {comparison.data?.selection_hash}）</span> : null}
            {copyError ? <span role="alert">复制失败，请允许剪贴板访问后重试。</span> : null}
          </div>

          <table className={styles.matrix} data-testid="compare-matrix">
            <caption className="muted">
              行标题固定；“不能直接比较”的格子不显示量化值。
            </caption>
            <thead>
              <tr>
                <th scope="col">维度</th>
                {selected.map((versionId) => (
                  <th key={versionId} scope="col">
                    {documents[versionId]?.paper_id ?? versionId}
                    <span className="muted"> · {versionId}</span>
                    <span className="muted">
                      {' '}
                      · {documents[versionId]?.publication_date ?? '日期未报告'}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((dimension) => (
                <tr key={dimension} data-dimension={dimension}>
                  <th scope="row">{DIMENSION_LABELS[dimension]}</th>
                  {selected.map((versionId) => {
                    const cell = comparison.data?.cells.find(
                      (entry) =>
                        entry.paper_version_id === versionId && entry.dimension === dimension,
                    );
                    return (
                      <td key={versionId} data-comparable={cell?.comparable ? 'true' : 'false'}>
                        {cellText(comparison.data?.cells, versionId, dimension)}
                        {cell?.comparability_reason ? (
                          <p className="muted" data-testid="not-comparable">
                            {cell.comparability_reason}
                          </p>
                        ) : null}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </AsyncBoundary>
      ) : null}

      <footer className={styles.footer}>
        <label>
          切换选择（逗号分隔的 paper_version_id）
          <input
            value={selected.join(',')}
            onChange={(event) =>
              setSelected(event.target.value.split(',').map((value) => value.trim()).filter(Boolean))
            }
          />
        </label>
        <Button variant="quiet" onClick={() => setParams(new URLSearchParams({ versions: selected.join(',') }))}>
          固定到 URL
        </Button>
      </footer>
    </section>
  );
}

/** Missing data is never rendered as 0 (docs/05 §缺失值). */
export function cellText(
  cells: { paper_version_id: string; dimension: string; value: unknown; missing_reason: string | null; comparable: boolean }[] | undefined,
  versionId: string,
  dimension: string,
): string {
  const cell = cells?.find(
    (entry) => entry.paper_version_id === versionId && entry.dimension === dimension,
  );
  if (!cell) return MISSING_LABELS.UNKNOWN as string;
  if (cell.missing_reason) return MISSING_LABELS[cell.missing_reason] ?? '未知';
  if (cell.value === null || cell.value === undefined) return MISSING_LABELS.NOT_REPORTED as string;
  return String(cell.value);
}
