/**
 * UX-035 — a re-verification request says "task created", never "finished", and
 * cannot be submitted twice.
 *
 * Requirement (docs/03 §S06): 请求复核…成功只说"复核任务已创建"，待真正完成才刷新科学
 * 状态；显示预计任务数与资源档位（未知则不填数）.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { reviewItem } from '../support/review-fixtures';

describe('UX-035 re-verification is accepted, not completed', () => {
  it('only claims that the task was created, with the operation id', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    const dialog = await screen.findByTestId('reverify-dialog');
    await user.click(within(dialog).getByRole('button', { name: '提交复核' }));

    const result = await screen.findByTestId('reverify-result');
    expect(result).toHaveTextContent('复核任务已创建');
    expect(result).toHaveTextContent('operation');
    // It must NOT claim the verification finished.
    expect(result).not.toHaveTextContent('复核完成');
    expect(result).not.toHaveTextContent('已复核');
    expect(result).toHaveTextContent('科学结论状态将在任务真正完成后刷新');
  });

  it('sends one operation for a double click (idempotency key)', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();

    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    const dialog = await screen.findByTestId('reverify-dialog');
    const submit = within(dialog).getByRole('button', { name: '提交复核' });
    await user.click(submit);
    await user.click(submit).catch(() => undefined); // the dialog closes on success

    await waitFor(() => expect(server.operations.length).toBeGreaterThan(0));
    expect(server.operations).toHaveLength(1);
    expect(server.operations[0]?.kind).toBe('reverify');
    const key = server.operations[0]?.idempotencyKey;
    expect(key).toContain('reverify:');
    expect(key).toContain('clm_disputed');
  });

  it('does not invent an ETA or a tier it does not know', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    const estimate = await screen.findByTestId('reverify-estimate');
    expect(estimate).toHaveTextContent('未知');
    expect(estimate.textContent ?? '').not.toMatch(/\d+\s*(分钟|秒|小时)/);
  });

  it('reports a failed submission without changing anything', async () => {
    const server = createFakeServer({
      reviewItems: [reviewItem()],
      failures: {
        'POST /v1/ui/operations': { status: 409, code: 'REVISION_CONFLICT', message: 'stale' },
      },
    });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    await user.click(
      within(await screen.findByTestId('reverify-dialog')).getByRole('button', { name: '提交复核' }),
    );

    const error = await screen.findByTestId('reverify-error');
    expect(error).toHaveTextContent('REVISION_CONFLICT');
    expect(error).toHaveTextContent('未改变任何结论');
    expect(screen.queryByTestId('reverify-result')).not.toBeInTheDocument();
  });

  it('offers an explicit scope instead of silently using the whole library', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '请求复核' }));
    const dialog = await screen.findByTestId('reverify-dialog');

    expect(within(dialog).getByLabelText('当前结论（1 项）')).toBeChecked();
    expect(within(dialog).getByLabelText('该版本高风险项')).not.toBeChecked();
    await user.click(within(dialog).getByLabelText('该版本高风险项'));
    await user.click(within(dialog).getByRole('button', { name: '提交复核' }));

    await waitFor(() => expect(server.operations).toHaveLength(1));
    expect(server.operations[0]?.payload.scope).toBe('VERSION_HIGH_RISK');
    // Even for the wider scope the request names explicit claim ids.
    expect(Array.isArray(server.operations[0]?.payload.claim_ids)).toBe(true);
    expect(server.operations[0]?.payload.claim_ids).toEqual(['clm_disputed']);
  });
});
