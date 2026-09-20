import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../src/app/App';
import { UploadQueue } from '../../src/state/queries';
import { Distribution } from '../../src/components/ui/Distribution';
import { createFakeServer } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';
import { createFakeEngine } from '../support/fake-engine';

const paper = { paper_id: 'pap_debug', title: 'Debug paper', paper_version_id: 'pver_debug' };
const pdf = (name: string) => new File(['%PDF-1.7 test'], name, { type: 'application/pdf' });

describe('UI workflow regression', () => {
  beforeEach(() => { sessionStorage.clear(); localStorage.clear(); window.history.replaceState({}, '', '/app/home'); });

  it('redirects the app root to a real home page', async () => {
    window.history.replaceState({}, '', '/app/');
    render(<App mode="TEST" fetchImpl={createFakeServer().fetch} />);
    await screen.findByRole('heading', { name: '研究桌面' });
    expect(window.location.pathname).toBe('/app/home');
  });

  it('submits palette searches and guards background shortcuts inside the modal', async () => {
    renderApp(createFakeServer({ papers: [paper] }), { path: '/app/home' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /命令与搜索/ }));
    const dialog = await screen.findByRole('dialog', { name: '命令与搜索' });
    const close = within(dialog).getByRole('button', { name: '关闭命令与搜索' });
    close.focus();
    await user.keyboard('f/');
    expect(document.querySelector('[data-focus]')).toHaveAttribute('data-focus', 'false');
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.type(within(dialog).getByRole('textbox'), 'Debug{Enter}');
    await screen.findByRole('heading', { name: '文献库' });
    expect(new URLSearchParams(window.location.search).get('q')).toBe('Debug');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await screen.findByRole('link', { name: 'Debug paper' });
  });

  it('keeps pending uploads across close/reopen and uses one batch for concurrent selections', async () => {
    const server = createFakeServer();
    const original = server.fetch;
    let release!: () => void;
    const barrier = new Promise<void>(resolve => { release = resolve; });
    server.fetch = (async (input, init) => {
      if (String(input).endsWith('/import-batches') && init?.method === 'POST') await barrier;
      return original(input, init);
    }) as typeof fetch;
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '导入 PDF' }));
    const input = screen.getByLabelText('选择文件');
    fireEvent.change(input, { target: { files: [pdf('first.pdf')] } });
    fireEvent.change(input, { target: { files: [pdf('second.pdf')] } });
    await user.click(screen.getByRole('button', { name: '关闭导入 PDF' }));
    release();
    await waitFor(() => expect(server.uploads).toHaveLength(2));
    await user.click(screen.getByRole('button', { name: '导入 PDF' }));
    expect(await screen.findAllByTestId('import-row-IMPORTED')).toHaveLength(2);
    expect(server.requests.filter(r => r.method === 'POST' && r.path === '/v1/ui/import-batches')).toHaveLength(1);
    expect(screen.getByLabelText('分析深度')).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '开始新一轮导入' }));
    expect(screen.getByLabelText('分析深度')).toBeEnabled();
    expect(screen.queryByTestId('import-row-IMPORTED')).not.toBeInTheDocument();
  });

  it('does not bypass local upload limits on retry', () => {
    const upload = vi.fn();
    const queue = new UploadQueue({ batchId: 'test', maxFileBytes: 2, upload, onChange: () => {} });
    queue.enqueueFiles([pdf('too-large.pdf')]);
    queue.retryFailed(queue.currentRows.map(row => row.key));
    expect(upload).not.toHaveBeenCalled();
    expect(queue.currentRows[0]).toMatchObject({ state: 'FAILED', retryable: false });
  });

  it('opens the stored page, updates the URL and saves an explicit reading bookmark', async () => {
    const server = createFakeServer({ papers: [paper], modules: [{ id: 'overview', claims: [] }, { id: 'method', claims: [] }] });
    const engine = createFakeEngine(12);
    renderApp(server, { path: '/app/papers/pap_debug?paper_version_id=pver_debug&reader=1&page=7', engineFactory: async () => engine });
    await waitForWorkspace();
    await waitFor(() => expect(engine.renderedPages).toContain(7));
    expect(screen.getByRole('spinbutton', { name: '页码' })).toHaveValue(7);
    expect(server.personalPatches).toHaveLength(0);
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(new URLSearchParams(location.search).get('page')).toBe('8'));
    await user.click(screen.getByRole('button', { name: '记住这一页' }));
    await waitFor(() => expect(server.personalPatches).toHaveLength(1));
    expect(server.personalPatches[0]?.body).toMatchObject({ reading_anchor: { paper_version_id: 'pver_debug', page_number: 8 }, expected_revision: 0, read_state: 'READING' });
    await user.click(screen.getByRole('tab', { name: '方法' }));
    expect(new URLSearchParams(location.search).get('tab')).toBe('method');
    await user.click(screen.getByRole('link', { name: '桌面' }));
    const continuation = await screen.findByRole('link', { name: /继续阅读/ });
    expect(continuation).toHaveAttribute('href', expect.stringContaining('reader=1&page=8'));
    await user.click(continuation);
    expect(await screen.findByRole('spinbutton', { name: '页码' })).toHaveValue(8);
  });

  it('restores the selected paper tab from a direct URL', async () => {
    renderApp(createFakeServer({ papers: [paper], modules: [{ id: 'method', claims: [{ claim_id: 'c1', statement: '真实方法步骤' }] }] }), { path: '/app/papers/pap_debug?tab=method' });
    await waitForWorkspace();
    expect(screen.getByRole('tab', { name: '方法' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('method-flow')).toHaveTextContent('真实方法步骤');
  });

  it('sends the chosen analysis depth rather than hardcoding full analysis', async () => {
    const server = createFakeServer({ papers: [paper] });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('checkbox', { name: '选择 Debug paper' }));
    await user.click(screen.getByRole('button', { name: '修改深度' }));
    const dialog = await screen.findByRole('dialog', { name: '确认范围' });
    await user.selectOptions(within(dialog).getByLabelText('分析深度'), 'T1_SCAN');
    await user.click(within(dialog).getByRole('button', { name: '确认执行' }));
    await waitFor(() => expect(server.operations).toHaveLength(1));
    expect(server.operations[0]?.payload).toEqual({ paper_ids: ['pap_debug'], tier: 'T1_SCAN' });
  });

  it('does not present a failed jobs request as an empty queue', async () => {
    renderApp(createFakeServer({ failures: { 'GET /v1/ui/jobs': { status: 400, code: 'TEST_JOBS_ERROR' } } }), { path: '/app/operations' });
    expect(await screen.findByText(/TEST_JOBS_ERROR/)).toBeInTheDocument();
    expect(screen.queryByText('没有任务。')).not.toBeInTheDocument();
    expect(screen.queryByRole('figure', { name: '处理概况' })).not.toBeInTheDocument();
  });

  it('renames with user input and fetches the actual latest revision on conflict', async () => {
    const saved = { saved_search_id: 's1', name: '原名', revision: 2, query: { query: '', kind: 'PAPERS', filters: {}, sort: 'RELEVANCE', cursor: null, limit: 50 } };
    const server = createFakeServer({ savedSearches: [saved], savedSearchConflict: true });
    const original = server.fetch;
    let reads = 0;
    server.fetch = (async (input, init) => {
      if (String(input).endsWith('/saved-searches') && (!init?.method || init.method === 'GET')) {
        reads++;
        if (reads > 1) { saved.revision = 9; saved.name = '远端已改名'; }
      }
      return original(input, init);
    }) as typeof fetch;
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '改名' }));
    expect(server.requests.filter(r => r.method === 'PATCH')).toHaveLength(0);
    await user.clear(screen.getByLabelText('新名称'));
    await user.type(screen.getByLabelText('新名称'), '我的新名称');
    await user.click(screen.getByRole('button', { name: '保存名称' }));
    const conflict = await screen.findByTestId('saved-conflict');
    expect(conflict).toHaveTextContent('我的新名称');
    expect(conflict).toHaveTextContent('远端已改名');
    await user.click(within(conflict).getByRole('button', { name: '用本地名称重试' }));
    await waitFor(() => expect(server.requests.filter(r => r.method === 'PATCH')).toHaveLength(2));
    expect(server.requests.filter(r => r.method === 'PATCH')[1]?.body).toMatchObject({ name: '我的新名称', expected_revision: 9 });
  });

  it('requires explicit tag selection and a chosen merge target', async () => {
    const server = createFakeServer({ papers: [{ ...paper, tags: [{ id: 'a', label: '标签甲', namespace: 'topic', is_candidate: false }, { id: 'b', label: '标签乙', namespace: 'topic', is_candidate: false }] }] });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    await screen.findByRole('link', { name: 'Debug paper' });
    await user.click(screen.getByText('整理标签', { selector: 'summary' }));
    const panel = screen.getByTestId('tag-actions');
    expect(within(panel).getByRole('button', { name: '确认所选标签（预览）' })).toBeDisabled();
    expect(within(panel).getByRole('button', { name: '预览标签归并' })).toBeDisabled();
    await user.click(within(panel).getByRole('checkbox', { name: '标签甲' }));
    await user.click(within(panel).getByRole('checkbox', { name: '标签乙' }));
    await user.selectOptions(within(panel).getByLabelText('归并到'), 'b');
    await user.click(within(panel).getByRole('button', { name: '预览标签归并' }));
    await screen.findByTestId('tag-preview');
    expect(server.requests.find(r => r.path === '/v1/ui/tags/actions')?.body).toMatchObject({ tag_ids: ['a', 'b'], target_tag_id: 'b', preview_only: true });
    await user.click(within(panel).getByRole('checkbox', { name: '标签乙' }));
    expect(screen.queryByTestId('tag-preview')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '确认执行' })).not.toBeInTheDocument();
  });

  it('shows unknown job states and counts only recorded subtasks', async () => {
    renderApp(createFakeServer({ jobs: [{ job_id: 'job1', paper_id: 'pap_debug', state: 'RUNNING', current_stage: 'ANALYZED', progress: { SUCCEEDED: 2, RUNNING: 1, FAILED: 1 } }, { job_id: 'job2', paper_id: 'pap_debug', state: 'FUTURE_STATE', progress: {} }] }), { path: '/app/operations' });
    const progress = await screen.findByRole('progressbar', { name: '已完成子任务' });
    expect(progress).toHaveAttribute('value', '2');
    expect(progress).toHaveAttribute('max', '4');
    expect(screen.getByRole('heading', { name: '其他状态（1）' })).toBeInTheDocument();
    expect(screen.getByText(/未知状态（FUTURE_STATE）/)).toBeInTheDocument();
  });

  it('uses actual distribution counts with a readable legend, including zeros', () => {
    render(<Distribution label="测试分布" items={[{ key: 'supported', label: '有支持', count: 3, tone: 'success' }, { key: 'pending', label: '待核查', count: 1, tone: 'warning' }, { key: 'none', label: '无证据', count: 0, tone: 'muted' }]} />);
    const figure = screen.getByRole('figure', { name: '测试分布' });
    expect(figure).toHaveTextContent('4合计');
    expect(figure).toHaveTextContent('有支持3');
    expect(figure).toHaveTextContent('无证据0');
    expect(figure.querySelectorAll('circle[stroke-dasharray]')).toHaveLength(2);
  });
});
