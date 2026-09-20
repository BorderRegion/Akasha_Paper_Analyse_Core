/**
 * UX-022 — batch actions preview their scope and are idempotent.
 *
 * Requirement (docs/03 §S02 + docs/05 §Mutation协议): the tray names EXPLICIT
 * paper ids (no implicit "whole library" action), the confirmation shows the
 * count and the scope, the request carries an Idempotency-Key so a repeated
 * click cannot create a second operation, and the receipt is reported as
 * "accepted", never as "done".
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

async function selectPapers(
  _server: ReturnType<typeof createFakeServer>,
  titles: string[],
): Promise<ReturnType<typeof userEvent.setup>> {
  const user = userEvent.setup();
  for (const title of titles) {
    await user.click(await screen.findByRole('checkbox', { name: `选择 ${title}` }));
  }
  await screen.findByTestId('selection-count');
  return user;
}

describe('UX-022 batch actions preview scope and stay idempotent', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.history.pushState({}, '', '/app/library');
  });

  it('previews the exact scope before executing', async () => {
    const server = createFakeServer({
      papers: [
        { paper_id: 'pap_1', title: 'First paper' },
        { paper_id: 'pap_2', title: 'Second paper' },
      ],
    });
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    const user = await selectPapers(server, ['First paper']);

    await user.click(screen.getByRole('button', { name: '修改深度' }));
    const preview = await screen.findByTestId('scope-preview');
    expect(preview).toHaveTextContent('将对 1 篇执行「修改深度」');
    expect(preview).toHaveTextContent('不含全库');
    expect(within(preview).getByText('pap_1')).toBeInTheDocument();
    expect(server.operations).toHaveLength(0);

    await user.click(within(preview).getByRole('button', { name: '确认执行' }));
    await waitFor(() => expect(server.operations).toHaveLength(1));
    const operation = server.operations[0];
    expect(operation?.kind).toBe('set_tier');
    expect(operation?.payload.paper_ids).toEqual(['pap_1']);
  });

  it('sends one idempotency key per action and scope, and reports "accepted"', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    const user = await selectPapers(server, ['First paper']);

    await user.click(screen.getByRole('button', { name: '导出' }));
    await user.click(within(await screen.findByTestId('scope-preview')).getByRole('button', {
      name: '确认执行',
    }));
    await waitFor(() => expect(server.operations).toHaveLength(1));

    const result = await screen.findByTestId('operation-result');
    expect(result).toHaveTextContent('已受理 operation');
    expect(result).toHaveTextContent('受理不代表执行成功');

    const firstKey = server.operations[0]?.idempotencyKey;
    expect(firstKey).toMatch(/^batch:export:/);
    expect(server.operations[0]?.payload.paper_version_ids).toEqual(['pver_pap_1']);
    expect(server.operations[0]?.payload).not.toHaveProperty('paper_ids');

    // A new confirmed export after success must include any newer data.
    await user.click(screen.getByRole('button', { name: '导出' }));
    await user.click(within(await screen.findByTestId('scope-preview')).getByRole('button', {
      name: '确认执行',
    }));
    await waitFor(() => expect(server.operations).toHaveLength(2));
    expect(server.operations[1]?.idempotencyKey).not.toBe(firstKey);

    // A different scope is a different operation.
    await user.click(screen.getByRole('checkbox', { name: '选择 First paper' })); // deselect
    await user.click(screen.getByRole('checkbox', { name: '选择 First paper' })); // select again
    await user.click(screen.getByRole('button', { name: '修改深度' }));
    await user.click(within(await screen.findByTestId('scope-preview')).getByRole('button', {
      name: '确认执行',
    }));
    await waitFor(() => expect(server.operations).toHaveLength(3));
    expect(server.operations[2]?.idempotencyKey).not.toBe(firstKey);
  });

  it('keeps the selection and explains a failed batch instead of showing success', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'First paper' }],
      failures: {
        'POST /v1/ui/operations': { status: 409, code: 'REVISION_CONFLICT', message: 'stale scope' },
      },
    });
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    const user = await selectPapers(server, ['First paper']);

    await user.click(screen.getByRole('button', { name: '修改深度' }));
    await user.click(
      within(await screen.findByTestId('scope-preview')).getByRole('button', { name: '确认执行' }),
    );

    const error = await screen.findByTestId('operation-error');
    expect(error).toHaveTextContent('REVISION_CONFLICT');
    expect(error).toHaveTextContent('核对执行结果');
    // The selection survives a failure so the user can retry deliberately.
    expect(screen.getByTestId('selection-count')).toHaveTextContent('已选择 1 篇');
    expect(screen.queryByTestId('operation-result')).not.toBeInTheDocument();
  });

  it('clears the tray explicitly and hides the batch bar when empty', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'First paper' }] });
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    const user = await selectPapers(server, ['First paper']);

    await user.click(screen.getByRole('button', { name: '取消选择' }));
    await waitFor(() => expect(screen.queryByLabelText('批量操作托盘')).not.toBeInTheDocument());
  });
});
