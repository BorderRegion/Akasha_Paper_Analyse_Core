/**
 * UX-026 — the method view uses only stored structure and degrades honestly.
 *
 * Requirement (docs/03 §S04): 方法页：输入→3—7个主要组件→输出 … 模型未生成结构时降级
 * 成编号步骤，不硬画虚假的流程图.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { MethodFlow } from '../../src/features/paper/PaperWorkspacePage';
import type { ClaimPreview } from '../../src/api/contract';
import { createFakeServer } from '../support/fake-server';

function claim(id: string, statement: string): ClaimPreview {
  return {
    claim_id: id,
    statement,
    claim_type: 'FACT',
    support_state: 'SUPPORTED',
    paper_version_id: 'pver_1',
    evidence_count: 1,
    is_superseded: false,
  };
}

async function renderPaper(server: ReturnType<typeof createFakeServer>): Promise<void> {
  window.history.pushState({}, '', '/app/papers/pap_1');
  render(<App mode="TEST" fetchImpl={server.fetch} />);
  await screen.findByTestId('pinned-version');
}

describe('UX-026 method flow only shows stored structure', () => {
  it('renders stored method claims as numbered steps with an explicit degradation note', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Method paper' }],
      modules: [
        {
          id: 'method',
          claims: [
            { claim_id: 'clm_1', statement: '输入：多光谱图像' },
            { claim_id: 'clm_2', statement: '组件：轻量编码器' },
            { claim_id: 'clm_3', statement: '输出：检测框' },
          ],
        },
      ],
    });
    await renderPaper(server);
    const user = (await import('@testing-library/user-event')).default.setup();
    await user.click(await screen.findByRole('tab', { name: '方法' }));

    const flow = await screen.findByTestId('method-flow');
    expect(flow).toHaveTextContent('输入：多光谱图像');
    expect(flow).toHaveTextContent('输出：检测框');
    expect(screen.getByTestId('method-degraded')).toHaveTextContent('按记录顺序列出方法步骤');
    expect(screen.getByTestId('method-degraded')).toHaveTextContent('依赖关系尚未整理');
    // Steps are ordered exactly as the server stored them.
    const steps = flow.querySelectorAll('li');
    expect([...steps].map((step) => step.textContent)).toEqual([
      '输入：多光谱图像',
      '组件：轻量编码器',
      '输出：检测框',
    ]);
  });

  it('draws nothing when the backend stored no method structure', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Method paper' }],
      modules: [{ id: 'method', availability: 'UNAVAILABLE', reason: '该模块尚无已存分析', claims: [] }],
    });
    await renderPaper(server);
    const user = (await import('@testing-library/user-event')).default.setup();
    await user.click(await screen.findByRole('tab', { name: '方法' }));

    expect(await screen.findByText(/该模块尚无已存分析/)).toBeInTheDocument();
    expect(screen.queryByTestId('method-flow')).not.toBeInTheDocument();
  });

  it('says so when the module exists but has no steps yet', () => {
    render(<MethodFlow claims={[]} />);
    expect(screen.getByText(/没有已保存的结构化步骤/)).toBeInTheDocument();
  });

  it('never invents edges: the component renders a list, not a graph with claims it does not have', () => {
    const { container } = render(
      <MethodFlow claims={[claim('clm_1', 'step one'), claim('clm_2', 'step two')]} />,
    );
    expect(container.querySelectorAll('li')).toHaveLength(2);
    // No arrow/edge markup is emitted for structure the server never sent.
    expect(container.innerHTML).not.toMatch(/edge|arrow|→/);
  });
});
