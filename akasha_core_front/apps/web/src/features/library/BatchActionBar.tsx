/**
 * BatchActionBar — scope preview + idempotent submit (docs/03 §S02, docs/05
 * §Mutation协议).
 *
 * "全库选择" is deliberately absent: a batch action always names explicit paper
 * ids. Submitting shows the count and the scope FIRST, sends one idempotency key
 * so a double click cannot create two operations, and reports the server's
 * per-target results instead of claiming everything worked.
 */

import { useRef, useState, type ReactElement } from 'react';
import { useNavigate } from 'react-router-dom';
import styles from './library.module.css';
import { Button } from '../../components/ui/Button';
import { Modal } from '../../components/ui/Modal';
import { useQueryClient } from '@tanstack/react-query';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import type { OperationResult } from '../../api/ui';

export interface BatchActionBarProps {
  selection: string[];
  onClear(): void;
  onRemovePaper(paperId: string): void;
}

type BatchKind = 'set_tier' | 'reanalyze' | 'export';

const ACTION_LABELS: Record<BatchKind, string> = {
  set_tier: '修改深度',
  reanalyze: '重新分析',
  export: '导出',
};

export function BatchActionBar({ selection, onClear }: BatchActionBarProps): ReactElement | null {
  const { api } = useSession();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [tier, setTier] = useState('T2_FULL');
  const [pending, setPending] = useState<BatchKind | null>(null);
  const [preview, setPreview] = useState<BatchKind | null>(null);
  const [result, setResult] = useState<OperationResult | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const attempts = useRef(new Map<string, string>());
  const submitting = useRef(false);

  if (!selection.length) return null;

  const submit = async (kind: BatchKind) => {
    if (submitting.current) return;
    submitting.current = true;
    setPending(kind);
    setErrorCode(null);
    try {
      const versionIds = kind === 'set_tier' ? [] : await Promise.all(
        selection.map(async (id) => (await api.workspace(id)).data.selected_version_id),
      );
      const scopeKey = `${kind}:${kind === 'export' ? 'JSON' : tier}:${[...(versionIds.length ? versionIds : selection)].sort().join(',')}`;
      const key = attempts.current.get(scopeKey) ?? `batch:${kind}:${crypto.randomUUID()}`;
      attempts.current.set(scopeKey, key);
      const response = await api.submitOperation({
        kind,
        payload:
          kind === 'export'
            ? { paper_version_ids: versionIds, format: 'JSON' }
            : kind === 'reanalyze'
              ? { paper_version_ids: versionIds, tier }
              : { paper_ids: selection, tier },
        // Retry an uncertain request with its key. A newly confirmed action
        // after success must be allowed to export/reanalyze newer data.
        idempotency_key: key,
      });
      attempts.current.delete(scopeKey);
      setResult(response.data);
      setPreview(null);
      void queryClient.invalidateQueries({ queryKey: ['ui', 'library'] });
      void queryClient.invalidateQueries({ queryKey: ['core', 'jobs'] });
    } catch (error) {
      setErrorCode(errorCodeOf(error) ?? 'INTERNAL_001');
    } finally {
      submitting.current = false;
      setPending(null);
    }
  };

  return (
    <aside className={styles.tray} aria-label="批量操作托盘">
      <p data-testid="selection-count">已选择 {selection.length} 篇（跨页保留）</p>
      <details><summary>查看所选编号</summary><ul className={styles.trayIds}>
        {selection.map((id) => (
          <li key={id}>{id}</li>
        ))}
      </ul></details>
      <div className={styles.trayActions}>
        <Button variant="quiet" disabled={selection.length < 2 || selection.length > 4} onClick={async () => {
          try {
            const versions = await Promise.all(selection.map(async id => (await api.workspace(id)).data.selected_version_id));
            navigate(`/app/compare?versions=${encodeURIComponent(versions.join(','))}`);
          } catch (error) { setErrorCode(errorCodeOf(error) ?? 'INTERNAL_001'); }
        }}>比较所选论文</Button>
        {(Object.keys(ACTION_LABELS) as BatchKind[]).map((kind) => (
          <Button key={kind} variant="quiet" onClick={() => { setErrorCode(null); setPreview(kind); }}>
            {ACTION_LABELS[kind]}
          </Button>
        ))}
        <Button variant="quiet" onClick={onClear}>
          取消选择
        </Button>
      </div>

      {preview ? (
        <Modal open title="确认范围" onClose={() => { if (!pending) setPreview(null); }} data-testid="scope-preview">
          <p>
            将对 {selection.length} 篇执行「{ACTION_LABELS[preview]}」，范围如下（仅这些论文 ID，不含全库）：
          </p>
          {preview !== 'export' ? <label>分析深度
            <select aria-label="分析深度" value={tier} disabled={pending !== null} onChange={event => setTier(event.target.value)}>
              <option value="T0_INDEX">仅索引（不调用模型）</option>
              <option value="T1_SCAN">快速扫描</option>
              <option value="T2_FULL">完整分析</option>
              <option value="T3_DEEP">深度分析</option>
            </select>
          </label> : null}
          {preview === 'set_tier' ? <p className="muted">仅修改深度设置。需要处理现有论文时，请另选「重新分析」。</p> : null}
          {preview === 'reanalyze' && tier !== 'T0_INDEX' ? <p className="muted">重新分析会调用模型服务，可能产生费用。</p> : null}
          <ul>
            {selection.map((id) => (
              <li key={id}>{id}</li>
            ))}
          </ul>
          <Button onClick={() => void submit(preview)} disabled={pending === preview}>
            {pending === preview ? '提交中…' : '确认执行'}
          </Button>
          <Button variant="quiet" disabled={pending !== null} onClick={() => setPreview(null)}>
            取消
          </Button>
          {errorCode ? <p role="alert" data-testid="operation-error">批量操作请求失败（{errorCode}），请在运行状态中核对执行结果。</p> : null}
        </Modal>
      ) : null}

      {result ? (
        <p role="status" data-testid="operation-result">
          已受理 operation {result.operation_id}（state={result.state}），
          {result.results.filter((item) => item.status === 'COMPLETED').length}/{result.results.length}{' '}
          项完成；受理不代表执行成功。
        </p>
      ) : null}
      {result?.kind === 'export' && ['COMPLETED', 'PARTIAL'].includes(result.state) ? (
        <a href={`/v1/ui/operations/${encodeURIComponent(result.operation_id)}/download`} download>
          下载 JSON 导出文件
        </a>
      ) : null}
      {errorCode && !preview ? (
        <p role="alert" data-testid="operation-error">
          批量操作请求失败（{errorCode}），请在运行状态中核对执行结果。
        </p>
      ) : null}
    </aside>
  );
}
