/**
 * UX-034 — the disputed original and the counter-evidence are reachable together.
 *
 * Requirement (docs/03 §S06 + docs/07 §5): 中：待查结论及精确证据；右：展开才出现运行
 * 来源/模型/验证记录. The claim's own evidence and the counter-evidence must be
 * reachable side by side, and the verification history must be visible without
 * overwriting anything.
 */

import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { reviewItem } from '../support/review-fixtures';

describe('UX-034 disputed claim and counter-evidence are side by side', () => {
  it('shows both sides at once, each with its evidence ids', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });

    const detail = await screen.findByTestId('review-detail');
    const original = within(detail).getByTestId('original-evidence');
    const counter = within(detail).getByTestId('counter-evidence');

    expect(original).toHaveTextContent('ev_a');
    expect(counter).toHaveTextContent('ev_b');
    // Both columns are rendered together: no drawer replaces the other side.
    expect(original.compareDocumentPosition(counter)).toBeTruthy();
  });

  it('says so when there is genuinely no counter-evidence', async () => {
    const server = createFakeServer({
      reviewItems: [reviewItem({ counter_evidence: [], group: 'NUMERIC_OR_OCR' })],
    });
    renderApp(server, { path: '/app/review' });
    const counter = within(await screen.findByTestId('review-detail')).getByTestId(
      'counter-evidence',
    );
    expect(counter).toHaveTextContent('目前没有找到反证');
  });

  it('keeps run and verification records behind an explicit expander', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await screen.findByTestId('review-detail');
    expect(screen.queryByTestId('verification-records')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('toggle-sources'));
    const records = await screen.findByTestId('verification-records');
    expect(records).toHaveTextContent('verification.contradiction');
    expect(records).toHaveTextContent('run_old');
    expect(records).toHaveTextContent('历史记录全部保留');
  });

  it('distinguishes "no verification returned" from "verified and clean"', async () => {
    const server = createFakeServer({
      reviewItems: [reviewItem({ verifications: [], group: 'NOT_VERIFIED' })],
    });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    const detail = await screen.findByTestId('review-detail');
    // The item's own pane names the group; the expanded records explain the
    // absence of verification rather than implying a clean result.
    expect(within(detail).getAllByText('未核查（审计未返回）').length).toBeGreaterThan(0);
    await user.click(screen.getByTestId('toggle-sources'));
    expect(await screen.findByTestId('verification-records')).toHaveTextContent(
      '审计服务未返回该结论的核查记录',
    );
  });

  it('lists the review reasons verbatim from the verifiers', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const detail = await screen.findByTestId('review-detail');
    expect(detail).toHaveTextContent('verification.contradiction → FAIL');
  });
});
