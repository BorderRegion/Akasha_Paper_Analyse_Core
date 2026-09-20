/**
 * UX-033 — "已看过" records a personal review decision, never a scientific change.
 *
 * Requirement (docs/03 §S06 + docs/06 §审查与外部Agent): "已看过"记录 user review
 * decision，不修改 support_state；争议仍在，用户可筛掉"已看过"的项目.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { reviewItem } from '../support/review-fixtures';

describe('UX-033 review decision is personal, not scientific', () => {
  it('records the decision with the claim revision and leaves support_state alone', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    const bar = await screen.findByRole('group', { name: '审查动作' });
    await user.click(within(bar).getByRole('button', { name: '已看过' }));

    await waitFor(() => expect(server.reviewDecisions).toHaveLength(1));
    expect(server.reviewDecisions[0]).toMatchObject({
      claim_id: 'clm_disputed',
      decision: 'SEEN',
      expected_claim_revision: 'rev-abc',
    });
    // The scientific state is untouched and still shown as disputed.
    expect(screen.getByTestId('review-detail')).toHaveTextContent('争议');
    const note = await screen.findByTestId('decision-note');
    expect(note).toHaveTextContent('已记录你的审查意见');
    expect(note).toHaveTextContent('未修改论文的科学结论');
  });

  it('never sends a support_state change with the decision', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    const bar = await screen.findByRole('group', { name: '审查动作' });
    await user.click(within(bar).getByRole('button', { name: '仍有疑问' }));

    await waitFor(() => expect(server.reviewDecisions).toHaveLength(1));
    const body = JSON.stringify(server.reviewDecisions[0]);
    expect(body).not.toContain('support_state');
    expect(server.reviewDecisions[0]?.decision).toBe('NEEDS_REVIEW');
  });

  it('reports a failed decision without pretending it was saved', async () => {
    const server = createFakeServer({
      reviewItems: [reviewItem()],
      reviewDecisionConflict: true,
    });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    const bar = await screen.findByRole('group', { name: '审查动作' });
    await user.click(within(bar).getByRole('button', { name: '已看过' }));

    const note = await screen.findByTestId('decision-note');
    expect(note).toHaveTextContent('未保存');
    expect(note).toHaveTextContent('REVISION_CONFLICT');
    expect(note).not.toHaveTextContent('已记录');
  });

  it('keeps seen items out of the default queue but reachable on request', async () => {
    const server = createFakeServer({
      reviewItems: [reviewItem({ personal_decision: 'SEEN' })],
    });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    expect(await screen.findByTestId('review-empty')).toHaveTextContent('审计服务未返回');
    await user.click(screen.getByLabelText('显示已看过的项'));
    expect(await screen.findByTestId('review-detail')).toHaveTextContent('mAP 提升 2.1 个百分点');
  });

  it('groups items by real error type and can filter by group', async () => {
    const server = createFakeServer({
      reviewItems: [
        reviewItem(),
        reviewItem({
          group: 'NOT_VERIFIED',
          impact: 'NORMAL',
          verifications: [],
          claim: {
            claim_id: 'clm_unverified',
            statement: 'no verification was returned',
            claim_type: 'FACT',
            support_state: 'SUPPORTED',
            paper_version_id: 'pver_1',
            evidence_count: 1,
            is_superseded: false,
          },
        }),
      ],
    });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await waitFor(() =>
      expect(screen.getByTestId('queue-summary')).toHaveTextContent('审计未返回 1 项'),
    );

    await user.click(screen.getByRole('button', { name: '未核查（审计未返回）' }));
    await waitFor(() =>
      expect(screen.getByTestId('review-statement')).toHaveTextContent('no verification was returned'),
    );
    // The clean/disputed item is filtered out, not silently merged.
    expect(screen.queryByText('mAP 提升 2.1 个百分点。')).not.toBeInTheDocument();
  });
});
