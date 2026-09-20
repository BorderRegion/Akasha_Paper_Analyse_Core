/**
 * UX-044 — saved searches and tag changes are recoverable on conflict.
 *
 * Requirement (docs/06 §专题与实体): 保存搜索：保存完整过滤结构及schema_version，不保存
 * 一次临时cursor；标签确认/别名归并需冲突提示与affected_count，不直接更改旧证据文本。
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const PAPER = { paper_id: 'pap_1', title: 'Detection study', tags: [{ id: 'tag_1', label: 'detection', namespace: 'topic', is_candidate: false }, { id: 'tag_2', label: 'detection-alias', namespace: 'topic', is_candidate: false }] };

async function selectTags(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByText('整理标签', { selector: 'summary' }));
  const panel = screen.getByTestId('tag-actions');
  for (const checkbox of within(panel).getAllByRole('checkbox')) await user.click(checkbox);
}

describe('UX-044 saved searches and tag actions recover from conflicts', () => {
  it('saves the full filter structure without a transient cursor', async () => {
    const server = createFakeServer({ papers: [PAPER] });
    renderApp(server, { path: '/app/library?q=detect&sort=TITLE&cursor=0' });
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'Detection study' });
    await user.type(screen.getByLabelText('名称'), '我的筛选');
    await user.click(screen.getByRole('button', { name: '保存当前筛选' }));

    await waitFor(() => {
      const created = server.requests.find(
        (request) => request.method === 'POST' && request.path === '/v1/ui/saved-searches',
      );
      expect(created).toBeTruthy();
      const body = created?.body as { name: string; query: { cursor?: string | null; query?: string } };
      expect(body.name).toBe('我的筛选');
      expect(body.query.query).toBe('detect');
      // A saved search must not carry the transient cursor.
      expect(body.query.cursor ?? null).toBeNull();
    });
  });

  it('applies a saved search back onto the listing', async () => {
    const server = createFakeServer({
      papers: [PAPER],
      savedSearches: [
        {
          saved_search_id: 'uss_1',
          name: '待核查目标检测',
          query: {
            query: 'detect',
            kind: 'PAPERS',
            filters: { audit_states: ['DISPUTED'] },
            sort: 'TITLE',
            cursor: null,
            limit: 50,
          },
          revision: 3,
        },
      ],
    });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '待核查目标检测' }));
    await waitFor(() => expect(window.location.search).toContain('audit=DISPUTED'));
    expect(window.location.search).toContain('q=detect');
  });

  it('keeps BOTH copies when a rename conflicts', async () => {
    const server = createFakeServer({
      papers: [PAPER],
      savedSearchConflict: true,
      savedSearches: [
        {
          saved_search_id: 'uss_1',
          name: '原名',
          query: { query: '', kind: 'PAPERS', filters: {}, sort: 'RELEVANCE', cursor: null, limit: 50 },
          revision: 2,
        },
      ],
    });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '改名' }));
    const name = screen.getByLabelText('新名称');
    await user.clear(name);
    await user.type(name, '新的检索名称');
    await user.click(screen.getByRole('button', { name: '保存名称' }));
    const conflict = await screen.findByTestId('saved-conflict');
    expect(conflict).toHaveTextContent('两份内容都保留');
    expect(conflict).toHaveTextContent('新的检索名称');
    expect(conflict).toHaveTextContent('原名');
    expect(within(conflict).getByRole('button', { name: /用本地名称重试/ })).toBeInTheDocument();
  });

  it('previews a tag action with the affected count before changing anything', async () => {
    const server = createFakeServer({ papers: [PAPER] });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'Detection study' });
    await selectTags(user);
    await user.click(screen.getByRole('button', { name: /确认所选标签/ }));

    const preview = await screen.findByTestId('tag-preview');
    expect(preview).toHaveTextContent('影响 3 篇论文');
    expect(preview).toHaveTextContent('预览：未做任何修改');
  });

  it('reports a tag conflict instead of merging silently', async () => {
    const server = createFakeServer({
      papers: [PAPER],
      tagAction: {
        operation_id: 'uop_tags',
        action: 'merge',
        state: 'PREVIEW',
        affected_count: 4,
        affected_paper_ids: ['pap_1'],
        tags: ['tag_1'],
        target_tag_id: 'tag_1',
        conflict: {
          reason: '不同命名空间的标签不能直接合并，需要人工确认目标语义',
          namespaces: ['method/', 'topic/'],
        },
        note: '预览：未做任何修改；确认后再执行。',
      },
    });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'Detection study' });
    await selectTags(user);
    await user.click(screen.getByRole('button', { name: '预览标签归并' }));

    const conflict = await screen.findByTestId('tag-conflict');
    expect(conflict).toHaveTextContent('不能直接合并');
    expect(conflict).toHaveTextContent('未做任何修改');
    expect(screen.queryByTestId('tag-applied')).not.toBeInTheDocument();
  });

  it('reports the applied count and that evidence text was not modified', async () => {
    const server = createFakeServer({
      papers: [PAPER],
      tagAction: {
        operation_id: 'uop_tags',
        action: 'confirm',
        state: 'COMPLETED',
        affected_count: 2,
        affected_paper_ids: ['pap_1', 'pap_2'],
        tags: ['tag_1'],
        target_tag_id: null,
        conflict: null,
        note: '标签语义已更新；旧证据文本未被修改。',
      },
    });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();

    await screen.findByRole('link', { name: 'Detection study' });
    await selectTags(user);
    await user.click(screen.getByRole('button', { name: /确认所选标签/ }));
    await user.click(await screen.findByRole('button', { name: '确认执行' }));

    const applied = await screen.findByTestId('tag-applied');
    expect(applied).toHaveTextContent('影响 2 篇');
    expect(applied).toHaveTextContent('旧证据文本未修改');
  });
});
