/**
 * UX-025 — every overview line is bound to a STORED claim.
 *
 * Requirement (docs/03 §S04): 概览首屏只回答：解决什么、核心办法、读它的价值、
 * 主要保留意见。每条均有小型"证据"按钮和性质标签；简短总结由已存分析生成，且关联
 * source_claim_ids；不在浏览器再次调用模型.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';

function renderPaper(server: ReturnType<typeof createFakeServer>, paperId = 'pap_1') {
  return renderApp(server, { path: `/app/papers/${paperId}` });
}

/** Wait until the workspace has loaded AND the version pinning has settled. */
const workspaceReady = waitForWorkspace;

describe('UX-025 overview claims are bound to stored claims', () => {
  it('renders one line per stored claim with its id, type and evidence button', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study' }],
      modules: [
        {
          id: 'overview',
          label: '概览',
          claims: [
            {
              claim_id: 'clm_a',
              statement: '解决小目标检测的召回率问题',
              claim_type: 'FACT',
              support_state: 'SUPPORTED',
              evidence_count: 3,
            },
            {
              claim_id: 'clm_b',
              statement: '在低照度下收益有限',
              claim_type: 'CRITIQUE',
              support_state: 'DISPUTED',
              evidence_count: 1,
            },
          ],
        },
      ],
    });
    renderPaper(server);
    await workspaceReady();

    const lines = await screen.findAllByText(/解决小目标检测的召回率问题|在低照度下收益有限/);
    expect(lines).toHaveLength(2);
    expect(document.querySelector('[data-claim-id="clm_a"]')).not.toBeNull();
    expect(document.querySelector('[data-claim-id="clm_b"]')).not.toBeNull();
    expect(await screen.findByRole('button', { name: '证据（3）' })).toBeInTheDocument();
    // The claim's nature is labelled from the frozen vocabulary, not implied by
    // colour alone.
    expect(screen.getByText('文中陈述')).toBeInTheDocument();
    expect(screen.getByText('研究评价')).toBeInTheDocument();
  });

  it('never calls a model from the browser while rendering the overview', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: 'stored line' }] }],
    });
    renderPaper(server);
    await workspaceReady();
    await screen.findByText('stored line');

    const modelCalls = server.requests.filter((request) =>
      /model|provider|chat|completion/i.test(request.path),
    );
    expect(modelCalls).toHaveLength(0);
  });

  it('shows the server reason when a module has no stored analysis', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study' }],
      modules: [
        {
          id: 'overview',
          availability: 'UNAVAILABLE',
          reason: '该模块尚无已存分析（未运行或该阶段被策略跳过）',
          claims: [],
        },
      ],
    });
    renderPaper(server);
    await workspaceReady();
    expect(await screen.findByText(/该模块尚无已存分析/)).toBeInTheDocument();
    expect(screen.getByText(/这部分内容暂时不可用/)).toBeInTheDocument();
  });

  it('reports the version the server pinned and keeps it in the URL', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study', paper_version_id: 'pver_9' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: 'stored line' }] }],
    });
    renderPaper(server);
    await workspaceReady();
    await screen.findByText('stored line');
    await waitFor(() => expect(window.location.search).toContain('paper_version_id=pver_9'));
    expect(await screen.findByTestId('pinned-version')).toHaveTextContent('pver_9');
  });

  it('keeps the selected claim while the evidence inspector is open and closed', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Detection study' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: 'stored line' }] }],
    });
    renderPaper(server);
    await workspaceReady();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '证据（1）' }));

    const inspector = await screen.findByTestId('evidence-inspector');
    expect(inspector).toHaveTextContent('stored line');
    expect(document.querySelector('[data-claim-id="clm_a"]')).toHaveAttribute('data-selected', 'true');

    const inspectorPanel = await screen.findByTestId('evidence-inspector');
    await user.click(within(inspectorPanel).getByRole('button', { name: '返回结论' }));
    expect(document.querySelector('[data-claim-id="clm_a"]')).toHaveAttribute('data-selected', 'false');
  });
});
