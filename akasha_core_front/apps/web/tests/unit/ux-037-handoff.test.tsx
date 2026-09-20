/**
 * UX-037 — the agent handoff carries a complete manifest, citations, hashes and
 * an enforced capacity limit.
 *
 * Requirement (docs/03 §S12 + docs/06 §审查与外部Agent): 预览包含摘要、未决问题、
 * priority claims、paper_version_id、source hashes、可调用工具、预算/内容范围；默认不包含
 * PDF二进制和原始模型完整请求；大文件显示体积并限额，先选择范围.
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { reviewItem } from '../support/review-fixtures';

async function openHandoff(server: ReturnType<typeof createFakeServer>) {
  renderApp(server, { path: '/app/review' });
  const user = userEvent.setup();
  await screen.findByTestId('review-detail');
  await user.click(screen.getByRole('button', { name: '预览交接内容' }));
  await screen.findByTestId('handoff-scope');
  return user;
}

describe('UX-037 agent handoff manifest, citations, hashes and capacity', () => {
  it('previews the scope and what will be included before generating', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    await openHandoff(server);

    expect(screen.getByTestId('handoff-scope')).toHaveTextContent('1 个明确版本');
    expect(screen.getByTestId('handoff-scope')).toHaveTextContent('不含全库');
    expect(screen.getByLabelText(/摘要与版本/)).toBeChecked();
    expect(screen.getByLabelText(/结论/)).toBeChecked();
    expect(screen.getByLabelText(/证据引用/)).toBeChecked();
    // PDF binaries and raw model output are off by default.
    expect(screen.getByLabelText(/PDF 原文/)).not.toBeChecked();
    expect(screen.getByLabelText(/模型原始输出/)).not.toBeChecked();
    expect(server.handoffs).toHaveLength(0);
  });

  it('generates a bundle with schema version, scope and source hashes', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    const user = await openHandoff(server);
    await user.click(screen.getByRole('button', { name: '生成交接包' }));

    const result = await screen.findByTestId('handoff-result');
    expect(result).toHaveTextContent('ho_fake_1');
    expect(result).toHaveTextContent('schema_version');
    expect(result).toHaveTextContent('source_hashes');
    expect(result).toHaveTextContent('pver_1');
    expect(server.handoffs[0]?.paper_version_ids).toEqual(['pver_1']);
    expect(server.handoffs[0]?.include).not.toContain('pdf');
  });

  it('includes the PDF only when the user explicitly opts in', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    const user = await openHandoff(server);
    await user.click(screen.getByLabelText(/PDF 原文/));
    await user.click(screen.getByRole('button', { name: '生成交接包' }));

    await waitFor(() => expect(server.handoffs).toHaveLength(1));
    expect(server.handoffs[0]?.include).toContain('pdf');
  });

  it('reports a capacity refusal with the real numbers and produces no partial file', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()], handoffTooLarge: true });
    const user = await openHandoff(server);
    const limit = screen.getByLabelText('体积上限（字节）');
    // A controlled number input: set the exact value rather than typing into it.
    fireEvent.change(limit, { target: { value: '1024' } });
    expect(limit).toHaveValue(1024);
    await user.click(screen.getByRole('button', { name: '生成交接包' }));

    const error = await screen.findByTestId('handoff-error');
    expect(error).toHaveTextContent('STORAGE_001');
    expect(error).toHaveTextContent('未产生部分文件');
    expect(screen.queryByTestId('handoff-result')).not.toBeInTheDocument();
    expect(server.handoffs[0]?.max_bytes).toBe(1024);
  });

  it('offers a small summary copy instead of pushing the whole bundle to the clipboard', async () => {
    const server = createFakeServer({ reviewItems: [reviewItem()] });
    const user = await openHandoff(server);
    await user.click(screen.getByRole('button', { name: '复制小摘要' }));

    const note = await screen.findByTestId('handoff-note');
    expect(note).toHaveTextContent('小摘要已复制');
    // userEvent installs a clipboard stub: read back what the page actually put
    // there, rather than trusting the note.
    const copied = await navigator.clipboard.readText();
    // The copied text is a SHORT summary (scope + includes + where the hashes
    // live), never the bundle itself.
    expect(copied).toContain('1 个明确版本');
    expect(copied).toContain('brief, claims, evidence_refs');
    expect(copied).toContain('document_sha256');
    expect(copied.length).toBeLessThan(500);
    expect(copied).not.toContain('mAP');
  });

  it('will not generate anything while the scope is empty', async () => {
    const server = createFakeServer({ reviewItems: [] });
    renderApp(server, { path: '/app/review' });
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '预览交接内容' }));
    const button = await screen.findByRole('button', { name: '生成交接包' });
    expect(button).toBeDisabled();
    expect(screen.getByText(/不会默认覆盖全库/)).toBeInTheDocument();
    await user.click(button);
    expect(server.handoffs).toHaveLength(0);
    expect(within(screen.getByTestId('handoff-panel')).getByTestId('handoff-scope')).toHaveTextContent(
      '0 个明确版本',
    );
  });
});
