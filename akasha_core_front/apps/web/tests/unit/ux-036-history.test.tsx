/**
 * UX-036 — a new verification keeps the OLD run and claim history.
 *
 * Requirement (docs/07 §5): 新复核与旧结论并排 diff，不能把旧结果覆盖后说"从来如此";
 * 原结论/旧审计保留 (docs/03 §S06).
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { reviewItem } from '../support/review-fixtures';

const HISTORY_ITEM = reviewItem({
  verifications: [
    {
      verification_id: 'ver_new',
      verifier_type: 'verification.numeric',
      status: 'FAIL',
      verdict: 'FAIL',
      reason_summary: 'numeric mismatch with cited evidence',
      run_id: 'run_new',
    },
    {
      verification_id: 'ver_old',
      verifier_type: 'verification.numeric',
      status: 'PASS',
      verdict: 'PASS',
      reason_summary: 'all numeric literals appear in cited evidence',
      run_id: 'run_old',
    },
  ],
  reasons: ['verification.numeric → FAIL: numeric mismatch with cited evidence'],
});

describe('UX-036 new verification keeps the old history', () => {
  it('lists every run, newest and oldest, without replacing either', async () => {
    const server = createFakeServer({ reviewItems: [HISTORY_ITEM] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await user.click(await screen.findByTestId('toggle-sources'));
    const records = await screen.findByTestId('verification-records');
    const rows = records.querySelectorAll('tbody tr');
    expect(rows).toHaveLength(2);
    expect(records).toHaveTextContent('run_new');
    expect(records).toHaveTextContent('run_old');
    // The older PASS is still visible next to the newer FAIL: nothing was
    // "always like this".
    expect(records).toHaveTextContent('all numeric literals appear in cited evidence');
    expect(records).toHaveTextContent('numeric mismatch with cited evidence');
  });

  it('shows each verification with its verifier type and verdict', async () => {
    const server = createFakeServer({ reviewItems: [HISTORY_ITEM] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    await user.click(await screen.findByTestId('toggle-sources'));
    const records = await screen.findByTestId('verification-records');

    const newest = within(records).getByText('run_new').closest('tr') as HTMLElement;
    expect(newest).toHaveTextContent('verification.numeric');
    expect(newest).toHaveTextContent('FAIL');
  });

  it('keeps the queue entry after a re-verification request (nothing is dropped optimistically)', async () => {
    const server = createFakeServer({ reviewItems: [HISTORY_ITEM] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    await user.click(
      within(await screen.findByTestId('reverify-dialog')).getByRole('button', { name: '提交复核' }),
    );
    await screen.findByTestId('reverify-result');

    // The claim is still in the queue with its DISPUTED state: an accepted task
    // is not a changed conclusion.
    const detail = screen.getByTestId('review-detail');
    expect(detail).toHaveTextContent('mAP 提升 2.1 个百分点');
    expect(detail).toHaveTextContent('争议');
    await waitFor(() => expect(server.operations).toHaveLength(1));
  });
});
