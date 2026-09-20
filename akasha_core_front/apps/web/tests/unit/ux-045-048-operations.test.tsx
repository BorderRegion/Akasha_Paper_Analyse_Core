/**
 * UX-045 — a module manifest is a CONFIG state, not health.
 * UX-046 — providers and workers are reported from real shared sampling.
 * UX-047 — an unknown ETA is null and carries its sample count.
 * UX-048 — disk statistics name the mount point and the overlap.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const SNAPSHOT = {
  queue: { pending: 2, running: 1, failed: 1 },
  workers: [{ identity: '(none)', last_seen: null, state: 'UNOBSERVED' }],
  modules: [
    {
      module_id: 'agents.builtin',
      health: 'UNKNOWN',
      observed_at: null,
      source: 'manifest',
      reason: 'manifest declares a healthcheck; no recorded result in this snapshot',
    },
  ],
  providers: [
    {
      id: 'prv_dlut',
      configured_limit: 40,
      observed_active: {
        value: null,
        unit: 'requests',
        state: 'UNKNOWN',
        observed_at: null,
        sample_count: 0,
        window_seconds: 300,
        reason: 'NOT_MEASURED',
      },
      latency_p50: { value: null, unit: 'ms', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
      latency_p90: { value: null, unit: 'ms', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
      error_rate: { value: null, unit: 'ratio', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
    },
  ],
  disk: [
    {
      mount_id: 'data',
      free_bytes: 10 * 1024 * 1024,
      total_bytes: 100 * 1024 * 1024,
      application_bytes: 3 * 1024 * 1024,
      observed_at: new Date().toISOString(),
      level: 'OK',
      measurable: true,
      device: '0x801',
      shares_mount_with: ['objects', 'cache', 'database'],
      reason: '对象/缓存/导出与数据库在同一挂载点，容量数字会重叠，不能相加',
    },
  ],
  eta_available: false,
  observed_at: new Date().toISOString(),
  unknown_reasons: ['No worker heartbeat recorded yet'],
};

const JOBS = [
  {
    job_id: 'job_1',
    paper_id: 'pap_1',
    state: 'RUNNING',
    current_stage: 'ANALYZED',
    tasks_completed: 3,
    tasks_planned: 9,
    eta: { p50_seconds: null, p90_seconds: null, includes_queue: true, sample_count: 0, reason: 'NOT_MEASURED' },
    queue_reason: null,
  },
  {
    job_id: 'job_2',
    paper_id: 'pap_2',
    state: 'FAILED',
    current_stage: 'EXTRACTED',
    tasks_completed: 1,
    tasks_planned: 5,
    queue_reason: 'provider rate limited',
  },
];

describe('UX-045 module health is not the manifest', () => {
  it('shows UNKNOWN with no observed_at and names the manifest as the source', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });

    const health = await screen.findByTestId('module-health');
    expect(health).toHaveTextContent('未观测');
    const row = health.closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('manifest');
    expect(row).toHaveTextContent('未观测');
    expect(screen.getByTestId('manifest-not-health')).toHaveTextContent('还没有收到运行检查结果');
  });
});

describe('UX-046 provider and worker observations come from real sampling', () => {
  it('separates the configured limit from the observed activity', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });

    const active = await screen.findByTestId('observed-active');
    expect(active).toHaveTextContent('未测量');
    expect(active).toHaveTextContent('UNKNOWN');
    const row = active.closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('40');
    // The window and sample count are shown, so a 0-sample metric is obvious.
    expect(row).toHaveTextContent('300s');
    expect(row).toHaveTextContent('0 样本');
  });

  it('reports an unobserved worker instead of inferring it from running tasks', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });
    const workers = await screen.findByTestId('workers');
    expect(workers).toHaveTextContent('未观测');
    // A running task exists, yet the worker count is still unknown.
    expect(JOBS.some((job) => job.state === 'RUNNING')).toBe(true);
  });
});

describe('UX-047 ETA is null until it can be calibrated', () => {
  it('shows 尚无法估算 with the reason and sample count', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });
    const etas = await screen.findAllByTestId('job-eta');
    expect(etas[0]).toHaveTextContent('尚无法估算');
    expect(etas[0]).toHaveTextContent('NOT_MEASURED');
    expect(etas[0]).toHaveTextContent('样本 0');
    expect(etas[0]?.textContent ?? '').not.toMatch(/^\d+s/);
  });

  it('shows a p50–p90 range WITH the sample count when the data exists', async () => {
    const server = createFakeServer({
      snapshot: SNAPSHOT,
      jobs: [
        {
          ...JOBS[0],
          eta: { p50_seconds: 120, p90_seconds: 300, includes_queue: true, sample_count: 12, reason: null },
        },
      ],
    });
    renderApp(server, { path: '/app/operations' });
    const eta = (await screen.findAllByTestId('job-eta'))[0];
    expect(eta).toHaveTextContent('120–300s');
    expect(eta).toHaveTextContent('含排队');
    expect(eta).toHaveTextContent('样本 12');
  });

  it('states that quantiles are not a completion guarantee', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });
    expect(await screen.findByText(/预计耗时仅供参考/)).toBeInTheDocument();
  });

  it('groups jobs into in-progress / needs-attention / finished', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });
    await screen.findByTestId('module-health');
    expect(screen.getByRole('heading', { name: '进行中（1）' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '需要处理（1）' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '已完成（0）' })).toBeInTheDocument();
  });

  it('shows job details without implying the API provided dependency edges', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });
    const user = userEvent.setup();
    await screen.findByTestId('module-health');

    expect(screen.queryByTestId('job-dag')).not.toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: '任务详情' })[0]!);
    const dag = await screen.findByTestId('job-dag');
    expect(dag).toHaveTextContent('暂未提供任务依赖图');
    expect(dag).toHaveTextContent('job_1');
  });
});

describe('UX-048 disk statistics name the mount and the overlap', () => {
  it('separates free space from the application usage and marks the shared mount', async () => {
    const server = createFakeServer({ snapshot: SNAPSHOT, jobs: JOBS });
    renderApp(server, { path: '/app/operations' });

    const free = await screen.findByTestId('mount-free');
    const app = screen.getByTestId('mount-app');
    expect(free).toHaveTextContent('10.0 MiB');
    expect(app).toHaveTextContent('3.0 MiB');
    expect(free.textContent).not.toBe(app.textContent);
    expect(screen.getByTestId('disk-overlap')).toHaveTextContent('不能相加');
    const row = free.closest('tr') as HTMLElement;
    expect(row).toHaveTextContent('objects');
    expect(row).toHaveTextContent('database');
  });

  it('reports an unreadable directory as not measurable, never 0 bytes', async () => {
    const server = createFakeServer({
      snapshot: {
        ...SNAPSHOT,
        disk: [
          {
            mount_id: 'data',
            free_bytes: null,
            total_bytes: null,
            application_bytes: null,
            observed_at: new Date().toISOString(),
            level: 'UNKNOWN',
            measurable: false,
            device: null,
            shares_mount_with: [],
            reason: '数据目录不可读取，无法测量（不是 0 字节）',
          },
        ],
      },
      jobs: JOBS,
    });
    renderApp(server, { path: '/app/operations' });
    const free = await screen.findByTestId('mount-free');
    expect(free).toHaveTextContent('不可测量');
    expect(free.textContent).not.toContain('0 B');
    await waitFor(() => expect(screen.getByTestId('disk-overlap')).toBeInTheDocument());
  });
});
