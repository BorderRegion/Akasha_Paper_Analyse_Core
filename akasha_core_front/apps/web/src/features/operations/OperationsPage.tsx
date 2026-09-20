/**
 * OperationsPage (docs/03 §S10 + docs/09).
 *
 * - first answer only: 进行中 / 需要处理 / 已完成 (no CPU dashboard by default);
 * - a row shows paper, stage, tasks done/planned, the queue wait reason and the
 *   actions that exist; the DAG appears only when the row is expanded and arrows
 *   mean DEPENDENCY, not completion percentage;
 * - ETA is null when it cannot be estimated: the UI shows "尚无法估算" with the
 *   sample count/window instead of a fake countdown;
 * - modules show their real health with observed_at and the source — a module
 *   manifest is a CONFIGURATION state, never "HEALTHY";
 * - providers separate configured limit from observed activity, with window and
 *   sample counts; a provider without samples is UNKNOWN, not 0;
 * - workers come from heartbeats: no heartbeat means "未观测", never inferred
 *   from running tasks;
 * - disk distinguishes free space from the application's own usage per mount and
 *   marks stores that share a mount (overlapping numbers are never stacked).
 */

import { useState, type ReactElement } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { StatusPill } from '../../components/ui/StatusPill';
import { Distribution } from '../../components/ui/Distribution';
import { errorCodeOf } from '../../lib/apiError';
import { formatBytes, MISSING_LABELS } from '../../lib/format';
import { HEALTH_LABELS } from '../../state/clientEvents';
import { useSession } from '../../state/session';
import type { OperationSnapshot } from '../../api/contract';
import styles from './operations.module.css';

interface JobRow {
  job_id: string;
  paper_id: string;
  state: string;
  current_stage: string;
  progress: Record<string, number>;
  eta?: { p50_seconds: number | null; p90_seconds: number | null; includes_queue: boolean; sample_count: number; reason: string | null };
  queue_reason?: string | null;
}

export function OperationsPage(): ReactElement {
  const { client, api } = useSession();
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [gcPreview, setGcPreview] = useState<{ operationId: string; scopeHash: string; files: number; bytes: number; expiresAt: string } | null>(null);
  const [gcError, setGcError] = useState<string | null>(null);
  const [gcResult, setGcResult] = useState<string | null>(null);

  const snapshot = useQuery({
    queryKey: ['ui', 'operations-snapshot'] as const,
    queryFn: async () => (await client.request<OperationSnapshot>('/v1/ui/operations/snapshot')).data,
    refetchInterval: 15000,
  });

  const jobs = useQuery({
    queryKey: ['core', 'jobs'] as const,
    refetchInterval: 15000,
    queryFn: async () => {
      const response = await client.request<{ jobs?: JobRow[] } | JobRow[]>('/v1/ui/jobs');
      const data = response.data;
      return Array.isArray(data) ? data : (data.jobs ?? []);
    },
  });

  const previewGc = useMutation({
    mutationFn: async () => (await api.submitOperation({ kind: 'gc_preview', payload: {} })).data,
    onSuccess: (data) => {
      const rows = data.results as unknown as { target_id: string; status: string; reason?: string }[];
      const scope = rows.find((row) => row.target_id === 'SCOPE');
      setGcPreview({
        operationId: data.operation_id,
        scopeHash: scope?.status ?? '',
        files: rows.filter((row) => row.target_id !== 'SCOPE').length,
        bytes: rows
          .filter((row) => row.target_id !== 'SCOPE')
          .reduce((sum, row) => sum + Number.parseInt(row.status, 10), 0),
        expiresAt: scope?.reason ?? '',
      });
      setGcError(null);
      setGcResult(null);
    },
    onError: (error) => setGcError(errorCodeOf(error)),
  });

  const executeGc = useMutation({
    mutationFn: async () =>
      (
        await api.submitOperation({
          kind: 'gc_execute',
          payload: { preview_id: gcPreview?.operationId, scope_hash: gcPreview?.scopeHash },
        })
      ).data,
    onSuccess: (data) => {
      setGcResult(`${data.state}：${data.results.length} 个对象`);
      setGcPreview(null);
      void queryClient.invalidateQueries({ queryKey: ['ui', 'operations-snapshot'] });
    },
    onError: (error) => setGcError(errorCodeOf(error)),
  });

  const data = snapshot.data;
  const active = (jobs.data ?? []).filter((job) => ['RUNNING', 'QUEUED', 'PENDING', 'RETRYING', 'WAITING'].includes(job.state));
  const needsAttention = (jobs.data ?? []).filter((job) => ['FAILED', 'BLOCKED'].includes(job.state));
  const finished = (jobs.data ?? []).filter((job) => ['SUCCEEDED', 'SUCCEEDED_WITH_WARNINGS', 'CANCELLED', 'SKIPPED'].includes(job.state));
  const other = (jobs.data ?? []).filter(job => ![...active, ...needsAttention, ...finished].includes(job));

  return (
    <section className={styles.page} aria-labelledby="operations-heading">
      <header className={styles.header}>
        <h1 id="operations-heading">运行状态</h1>
        <p className="muted">
          看看论文处理到哪一步了。预计耗时仅供参考，没有足够记录时会显示「尚无法估算」。
        </p>
      </header>

      <section className={styles.block} aria-labelledby="jobs-heading">
        <h2 id="jobs-heading">任务</h2>
        <AsyncBoundary state={jobs.isPending ? 'loading' : jobs.isError ? 'error' : 'ready'} errorCode={errorCodeOf(jobs.error)} onRetry={() => void jobs.refetch()}>
        {jobs.data ? <>
        <Distribution label="处理概况" items={[
          { key: 'active', label: '进行中', count: active.length, tone: 'info' },
          { key: 'attention', label: '需要处理', count: needsAttention.length, tone: 'warning' },
          { key: 'finished', label: '已结束', count: finished.length, tone: 'success' },
          ...(other.length ? [{ key: 'other', label: '其他状态', count: other.length, tone: 'muted' as const }] : []),
        ]} />
        <h3>进行中（{active.length}）</h3>
        <JobTable rows={active} expanded={expanded} onToggle={setExpanded} />
        <h3>需要处理（{needsAttention.length}）</h3>
        <JobTable rows={needsAttention} expanded={expanded} onToggle={setExpanded} />
        <h3>已完成（{finished.length}）</h3>
        <JobTable rows={finished} expanded={expanded} onToggle={setExpanded} />
        {other.length ? <><h3>其他状态（{other.length}）</h3><JobTable rows={other} expanded={expanded} onToggle={setExpanded} /></> : null}
        </> : null}
        </AsyncBoundary>
      </section>

      <AsyncBoundary
        state={snapshot.isPending ? 'loading' : snapshot.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(snapshot.error)}
        scopeLabel="运行观测"
        onRetry={() => void snapshot.refetch()}
      >
        {data ? (
          <div className={styles.observations}>
            <section className={styles.block} aria-labelledby="modules-heading">
              <h2 id="modules-heading">模块</h2>
              <p className="muted" data-testid="manifest-not-health">
                已配置的模块列在下面。「未观测」表示还没有收到运行检查结果。
              </p>
              <div className={styles.moduleSummary} role="group" aria-label="模块观测概况">
                {Object.entries(data.modules.reduce<Record<string, number>>((counts, module) => { counts[module.health] = (counts[module.health] ?? 0) + 1; return counts; }, {})).map(([state, count]) => <span key={state}><strong>{count}</strong>{HEALTH_LABELS[state as 'UNKNOWN'] ?? state}</span>)}
              </div>
              <details><summary>查看模块明细（{data.modules.length}）</summary>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">模块</th>
                    <th scope="col">健康</th>
                    <th scope="col">观测时间</th>
                    <th scope="col">来源</th>
                    <th scope="col">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {data.modules.map((module) => (
                    <tr key={module.module_id} data-module={module.module_id}>
                      <td>{module.module_id}</td>
                      <td data-testid="module-health">{HEALTH_LABELS[module.health as 'UNKNOWN'] ?? module.health}</td>
                      <td>{module.observed_at ?? '未观测'}</td>
                      <td>{module.source}</td>
                      <td className="muted">{module.reason ?? '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              </details>
            </section>

            <section className={styles.block} aria-labelledby="providers-heading">
              <h2 id="providers-heading">模型服务与后台任务</h2>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">模型服务</th>
                    <th scope="col">配置额度</th>
                    <th scope="col">当前活跃</th>
                    <th scope="col">p50 / p90</th>
                    <th scope="col">错误率</th>
                    <th scope="col">窗口 / 样本</th>
                  </tr>
                </thead>
                <tbody>
                  {data.providers.map((provider) => (
                    <tr key={provider.id} data-provider={provider.id}>
                      <td>{provider.id}</td>
                      <td>{provider.configured_limit ?? '未配置'}</td>
                      <td data-testid="observed-active">
                        {provider.observed_active.value ?? MISSING_LABELS.NOT_MEASURED}
                        <span className="muted">（{provider.observed_active.state}）</span>
                      </td>
                      <td>
                        {provider.latency_p50.value === null ? '未测量' : `${Math.round(provider.latency_p50.value)} ${provider.latency_p50.unit}`} / {provider.latency_p90.value === null ? '未测量' : `${Math.round(provider.latency_p90.value)} ${provider.latency_p90.unit}`}
                      </td>
                      <td>{provider.error_rate.value === null ? '未测量' : provider.error_rate.unit === 'ratio' ? `${(provider.error_rate.value * 100).toFixed(1)}%` : `${provider.error_rate.value} ${provider.error_rate.unit}`}</td>
                      <td>
                        {provider.observed_active.window_seconds ?? '—'}s /{' '}
                        {provider.observed_active.sample_count} 样本
                      </td>
                    </tr>
                  ))}
                  {!data.providers.length ? (
                    <tr>
                      <td colSpan={6}>暂时没有模型调用记录。</td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
              <ul className={styles.workers} data-testid="workers">
                {data.workers.map((worker) => (
                  <li key={worker.identity}>
                    worker {worker.identity} · 最后心跳 {worker.last_seen ?? '未观测'} · {worker.state}
                  </li>
                ))}
              </ul>
            </section>

            <section className={styles.block} aria-labelledby="disk-heading">
              <h2 id="disk-heading">磁盘</h2>
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th scope="col">挂载</th>
                    <th scope="col">可用空间</th>
                    <th scope="col">Akasha 占用</th>
                    <th scope="col">同挂载点</th>
                  </tr>
                </thead>
                <tbody>
                  {data.disk.map((mount) => (
                    <tr key={mount.mount_id} data-mount={mount.mount_id}>
                      <td>
                        {mount.mount_id}
                        {mount.device ? <span className="muted"> ({mount.device})</span> : null}
                      </td>
                      <td data-testid="mount-free">
                        {mount.measurable ? formatBytes(mount.free_bytes) : '不可测量（不是 0 字节）'}
                        {mount.measurable && mount.total_bytes && mount.free_bytes !== null ? <meter className={styles.diskMeter} min={0} max={mount.total_bytes} value={mount.total_bytes - mount.free_bytes} aria-label={`${mount.mount_id} 磁盘已用空间`} /> : null}
                      </td>
                      <td data-testid="mount-app">{formatBytes(mount.application_bytes)}</td>
                      <td>
                        {(mount.shares_mount_with ?? []).length
                          ? (mount.shares_mount_with ?? []).join(', ')
                          : '未报告'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.disk.some((mount) => mount.reason) ? (
                <p className="muted" data-testid="disk-overlap">
                  {data.disk.find((mount) => mount.reason)?.reason}
                </p>
              ) : null}
            </section>
          </div>
        ) : null}
      </AsyncBoundary>

      <section className={styles.block} aria-labelledby="gc-heading">
        <h2 id="gc-heading">清理（GC）</h2>
        <p className="muted">
          先预览，再确认。不会删除 PDF、证据或笔记。
        </p>
        <div className={styles.gcActions}>
          <Button disabled={previewGc.isPending} onClick={() => previewGc.mutate()}>
            预览可清理对象
          </Button>
        </div>
        {gcPreview ? (
          <div role="dialog" aria-label="确认清理范围" data-testid="gc-preview">
            <p>
              候选 {gcPreview.files} 个对象，合计约 {formatBytes(gcPreview.bytes)}；scope hash{' '}
              {gcPreview.scopeHash}；有效期至 {gcPreview.expiresAt}。
            </p>
            <Button disabled={executeGc.isPending} onClick={() => executeGc.mutate()}>
              确认清理这些对象
            </Button>
            <Button variant="quiet" onClick={() => setGcPreview(null)}>
              取消
            </Button>
          </div>
        ) : null}
        {gcError ? (
          <p role="alert" data-testid="gc-error">
            清理未执行（{gcError}）。范围过期或已变化时必须重新预览。
          </p>
        ) : null}
        {gcResult ? (
          <p role="status" data-testid="gc-result">
            清理完成：{gcResult}（活动任务引用的对象不会进入候选）
          </p>
        ) : null}
      </section>
    </section>
  );
}

function JobTable({
  rows,
  expanded,
  onToggle,
}: {
  rows: JobRow[];
  expanded: string | null;
  onToggle(jobId: string | null): void;
}): ReactElement {
  if (!rows.length) return <p className="muted">没有任务。</p>;
  return (
    <table className={styles.table}>
      <thead>
        <tr>
          <th scope="col">论文</th>
          <th scope="col">阶段</th>
          <th scope="col">任务</th>
          <th scope="col">状态</th>
          <th scope="col">预计耗时</th>
          <th scope="col" />
        </tr>
      </thead>
      <tbody>
        {rows.map((job) => (
          <JobRows key={job.job_id} job={job} expanded={expanded === job.job_id} onToggle={onToggle} />
        ))}
        {!rows.length ? (
          <tr>
            <td colSpan={6} className="muted">
              没有任务。
            </td>
          </tr>
        ) : null}
      </tbody>
    </table>
  );
}

function JobRows({
  job,
  expanded,
  onToggle,
}: {
  job: JobRow;
  expanded: boolean;
  onToggle(jobId: string | null): void;
}): ReactElement {
  const eta = job.eta;
  const counts = Object.values(job.progress ?? {});
  const total = counts.reduce((sum, count) => sum + count, 0);
  const done = ['SUCCEEDED', 'SUCCEEDED_WITH_WARNINGS', 'SKIPPED'].reduce((sum, state) => sum + (job.progress?.[state] ?? 0), 0);
  const stage = ({ EXTRACTED: '解析原文', ANALYZED: '分析内容', VERIFIED: '核查证据', INDEXED: '整理索引', SEARCH_INDEXED: '整理索引', INGESTED: '导入文件' } as Record<string, string>)[job.current_stage] ?? job.current_stage;
  return (
    <>
      <tr data-job-id={job.job_id}>
        <td><Link to={`/app/papers/${encodeURIComponent(job.paper_id)}`} title={job.paper_id}>打开论文 <span className={styles.shortId}>{job.paper_id.slice(-6)}</span></Link></td>
        <td>{stage}</td>
        <td>
          {total > 0 ? <div className={styles.progress}><progress aria-label="已完成子任务" value={done} max={total} /><span>{done}/{total}</span></div> : <span className="muted">未报告</span>}
        </td>
        <td>
          <StatusPill family="task" value={job.state} />
        </td>
        <td data-testid="job-eta">
          {eta?.p50_seconds == null ? '尚无法估算' : `${eta.p50_seconds}–${eta.p90_seconds ?? eta.p50_seconds}s${
                eta.includes_queue ? '（含排队）' : '（不含排队）'
              }`}
          <details className={styles.etaDetail}><summary>估算依据</summary>{eta?.reason ?? (eta ? '历史耗时分布' : 'NOT_MEASURED')} · 样本 {eta?.sample_count ?? 0}</details>
        </td>
        <td>
          <Button variant="quiet" onClick={() => onToggle(expanded ? null : job.job_id)} aria-expanded={expanded}>
            {expanded ? '收起' : '任务详情'}
          </Button>
        </td>
      </tr>
      {expanded ? (
        <tr data-testid="job-dag">
          <td colSpan={6}>
            <p className="muted">
              队列等待原因：
              {job.queue_reason ?? '未报告'}
            </p>
            <p className="muted">任务编号：{job.job_id} · 论文编号：{job.paper_id}</p>
            <p className="muted">进度按已记录的子任务计数，不代表剩余耗时；暂未提供任务依赖图。</p>
          </td>
        </tr>
      ) : null}
    </>
  );
}
