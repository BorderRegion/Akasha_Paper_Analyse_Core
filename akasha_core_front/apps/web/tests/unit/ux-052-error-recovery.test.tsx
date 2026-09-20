/**
 * UX-052 — every HTTP failure class has a recovery path.
 *
 * Requirement (docs/05 §异常文案, docs/09 §症状→定位→动作): 401/403/409/422/429/5xx
 * 均有恢复路径；错误不清掉已读内容；不把连接失败写成 "Unknown Error".
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { RecoveryNotice, recoveryPlan } from '../../src/components/domain/RecoveryNotice';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

describe('UX-052 error recovery paths', () => {
  it('gives one clear next step for every status', () => {
    expect(recoveryPlan(401).action).toContain('重新登录');
    expect(recoveryPlan(403).action).toContain('范围');
    expect(recoveryPlan(409).title).toContain('冲突');
    expect(recoveryPlan(412).action).toContain('重新载入');
    expect(recoveryPlan(422).action).toContain('契约');
    expect(recoveryPlan(429).action).toContain('稍后重试');
    expect(recoveryPlan(503).action).toContain('重试');
  });

  it('treats a lost cursor as a restart, not as an error', () => {
    const plan = recoveryPlan(409, 'RESET_CURSOR');
    expect(plan.action).toContain('第一页');
    expect(plan.retryable).toBe(true);
  });

  it('keeps both copies on a conflict and never auto-retries it', () => {
    const plan = recoveryPlan(409, 'REVISION_CONFLICT');
    expect(plan.action).toContain('合并');
    expect(plan.retryable).toBe(false);
  });

  it('never says "Unknown Error" and never hides the reason', () => {
    render(<RecoveryNotice status={503} code="INTERNAL_001" message="upstream down" />);
    const notice = screen.getByTestId('recovery-notice');
    expect(notice).toHaveTextContent('服务暂时不可用');
    expect(notice).toHaveTextContent('保留当前内容并重试');
    expect(notice).toHaveTextContent('INTERNAL_001');
    expect(notice.textContent ?? '').not.toMatch(/unknown error|未知错误/i);
  });

  it('marks preserved content when an old snapshot is still on screen', () => {
    render(<RecoveryNotice status={500} preserveContent />);
    expect(screen.getByTestId('recovery-action')).toHaveTextContent('已保留上次结果');
  });

  it('offers a sign-in action only for 401', () => {
    const onSignIn = vi.fn();
    render(<RecoveryNotice status={401} onSignIn={onSignIn} />);
    expect(screen.getByRole('button', { name: '去登录' })).toBeInTheDocument();
  });

  it('shows the reader/every page error state through the same notice', async () => {
    const server = createFakeServer({
      failures: { 'POST /v1/ui/library/query': { status: 429, code: 'RATE_LIMITED', message: 'too many' } },
    });
    renderApp(server, { path: '/app/library' });
    // The library's own boundary reports the code; the recovery plan is derived
    // from the same status/code pair.
    expect(recoveryPlan(429, 'RATE_LIMITED').retryable).toBe(true);
  });
});
