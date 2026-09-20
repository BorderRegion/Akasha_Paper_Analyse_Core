/**
 * UX-038 — a collection snapshot carries its scope and sample size.
 *
 * Requirement (docs/03 §S07): 返回的数据标 collection_id、selection_hash、
 * snapshot_at、样本数量和时间范围；筛选子集结论不能写成全领域趋势；不得因每次打开
 * 专题自动产生LLM费用.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const COLLECTION = {
  collection_id: 'col_1',
  title: 'Small object detection',
  selection_hash: 'sel-abc123',
  selected_paper_count: 7,
  analyzed_paper_count: 5,
  analysis_revision: 'rev-9',
  sample: { paper_versions: 8, claims: 21, years: [2019, 2024] },
  insights: [
    {
      title: '存在争议',
      claim_type: 'FACT',
      statement: '两个协议下的 mAP 提升不一致',
      source_refs: [],
    },
  ],
};

describe('UX-038 collection snapshot states its scope and sample', () => {
  it('shows collection id, selection hash, sample size and the year range', async () => {
    const server = createFakeServer({ collection: COLLECTION });
    renderApp(server, { path: '/app/collections/col_1' });

    const scope = await screen.findByTestId('collection-scope');
    expect(scope).toHaveTextContent('col_1');
    expect(scope).toHaveTextContent('sel-abc123');
    expect(scope).toHaveTextContent('7 篇');
    expect(scope).toHaveTextContent('5 篇');
    expect(scope).toHaveTextContent('21 条结论');
    expect(scope).toHaveTextContent('2019–2024');
  });

  it('labels the conclusions as subset-only', async () => {
    const server = createFakeServer({ collection: COLLECTION });
    renderApp(server, { path: '/app/collections/col_1' });
    expect(await screen.findByTestId('subset-notice')).toHaveTextContent('不能写成全领域趋势');
  });

  it('never starts analysis just by opening the collection', async () => {
    const server = createFakeServer({ collection: COLLECTION });
    renderApp(server, { path: '/app/collections/col_1' });
    await screen.findByTestId('collection-scope');
    await new Promise((resolve) => setTimeout(resolve, 50));
    // No model/operation request is issued by merely rendering the page.
    expect(server.operations).toHaveLength(0);
  });

  it('only generates observations on an explicit click, and reports acceptance', async () => {
    const server = createFakeServer({ collection: COLLECTION });
    renderApp(server, { path: '/app/collections/col_1' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: /生成专题观察/ }));
    await waitFor(() => expect(server.operations).toHaveLength(1));
    expect(server.operations[0]?.kind).toBe('corpus_refresh');
    expect(server.operations[0]?.payload.collection_id).toBe('col_1');
    const result = await screen.findByTestId('generate-result');
    expect(result).toHaveTextContent('已受理');
    expect(result).toHaveTextContent('完成后才会刷新结论');
  });

  it('says "not extracted" instead of "no problems" when nothing is stored', async () => {
    const server = createFakeServer({
      collection: { ...COLLECTION, insights: [], selected_paper_count: 0, analyzed_paper_count: 0 },
    });
    renderApp(server, { path: '/app/collections/col_1' });
    expect(await screen.findByTestId('no-insights')).toHaveTextContent('未提取');
  });
});
