/**
 * UX-039 — technique cards keep problem / preconditions / cost / sources.
 *
 * Requirement (docs/03 §S08): 工具式卡片：要解决的问题 → 手段 → 适用前提 → 收益/代价 →
 * 证据/来源篇数；按问题与条件检索；一键加入专题笔记时保存原条件/证据引用.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const ENTITY = {
  entity_id: 'ent_1',
  entity_type: 'TECHNIQUE',
  name: '梯度裁剪 + 学习率预热',
  aliases: ['gradient clipping'],
  description: { value: '缓解路由训练早期的梯度爆炸', missing_reason: null, source_refs: [] },
  relations: [],
  source_refs: [{ paper_id: 'pap_1', paper_version_id: null, claim_id: null, evidence_ids: [] }],
};

describe('UX-039 technique cards keep their conditions and sources', () => {
  it('shows problem, preconditions, cost and the number of source papers', async () => {
    const server = createFakeServer({ entities: [ENTITY] });
    renderApp(server, { path: '/app/techniques' });

    const card = (await screen.findByTestId('technique-cards')).querySelector('li') as HTMLElement;
    expect(card).toHaveTextContent('解决什么问题');
    expect(card).toHaveTextContent('缓解路由训练早期的梯度爆炸');
    expect(card).toHaveTextContent('适用前提');
    expect(card).toHaveTextContent('收益 / 代价');
    expect(card).toHaveTextContent('1 篇论文');
    // Unknown structure is stated, not invented.
    expect(card).toHaveTextContent('未结构化');
    expect(card).toHaveTextContent('未报告');
  });

  it('searches by problem/condition, not only by name', async () => {
    const server = createFakeServer({ entities: [ENTITY] });
    renderApp(server, { path: '/app/techniques' });
    const user = userEvent.setup();
    await screen.findByTestId('technique-cards');

    await user.type(screen.getByLabelText('按问题或条件检索'), '路由');
    await waitFor(() => {
      const query = server.requests.filter((request) => request.path === '/v1/ui/entities/query').at(-1);
      expect((query?.body as { query?: string } | undefined)?.query).toBe('路由');
    });
  });

  it('saves the technique with its original reference when added to notes', async () => {
    const server = createFakeServer({ entities: [ENTITY] });
    renderApp(server, { path: '/app/techniques' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: /加入专题笔记/ }));

    await waitFor(() => expect(screen.getByTestId('technique-note')).toBeInTheDocument());
    const note = server.requests.find((request) => request.method === 'POST' && request.path === '/v1/ui/notes');
    expect(JSON.stringify(note?.body)).toContain('ent_1');
    expect(JSON.stringify(note?.body)).toContain('梯度裁剪');
  });

  it('states an empty result instead of showing unrelated techniques', async () => {
    const server = createFakeServer({ entities: [] });
    renderApp(server, { path: '/app/techniques' });
    expect(await screen.findByTestId('technique-empty')).toHaveTextContent('问题/条件');
  });
});
