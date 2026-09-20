import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AsyncBoundary, type DataState } from '../../src/components/domain/AsyncBoundary';
import { clearUnknownStatusValues, resolveStatus, unknownStatusValues } from '../../src/lib/status';

const ALL_STATES: DataState[] = [
  'loading',
  'ready',
  'empty',
  'partial',
  'stale',
  'error',
  'forbidden',
  'capability_missing',
];

describe('UX-010 data state vocabulary', () => {
  it.each(ALL_STATES)('renders the %s state with a truthful message', (state) => {
    render(
      <AsyncBoundary
        state={state}
        errorCode="STORAGE_001"
        scopeLabel="本专题"
        capability="ui.export"
        lastUpdatedAt="2026-09-18 10:00"
      >
        {state === 'ready' || state === 'stale' || state === 'partial' || state === 'error' ? (
          <p>已有内容</p>
        ) : null}
      </AsyncBoundary>,
    );
    expect(document.querySelector(`[data-state="${state}"]`)).not.toBeNull();
  });

  it('never turns an error into an empty list', () => {
    render(<AsyncBoundary state="error" errorCode="INTERNAL_001" />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('请求失败');
    expect(alert).toHaveTextContent('INTERNAL_001');
    expect(alert.textContent).not.toContain('没有内容');
  });

  it('keeps prior content visible while stale and says so', () => {
    render(
      <AsyncBoundary state="stale" lastUpdatedAt="2026-09-18 10:00">
        <p>上次的结果</p>
      </AsyncBoundary>,
    );
    expect(screen.getByText('上次的结果')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('仍显示上次结果');
  });

  it('distinguishes forbidden from empty', () => {
    render(<AsyncBoundary state="forbidden" />);
    expect(screen.getByRole('alert')).toHaveTextContent('需要登录，或当前账户没有访问权限');
    expect(screen.getByRole('alert').textContent).not.toContain('没有内容');
  });

  it('names the missing capability without implying a retry will help', () => {
    render(<AsyncBoundary state="capability_missing" capability="ui.compare" />);
    const node = screen.getByRole('status');
    expect(node).toHaveTextContent('暂时不可用');
    expect(node).toHaveTextContent('ui.compare');
    expect(screen.queryByRole('button', { name: /重试/ })).not.toBeInTheDocument();
  });

  it('states the scope for an empty result', () => {
    render(<AsyncBoundary state="empty" scopeLabel="本专题 · 已读" />);
    expect(screen.getByText(/当前范围内没有内容（本专题 · 已读）/)).toBeInTheDocument();
  });

  it('exposes a skeleton only during loading, with a live label', () => {
    render(<AsyncBoundary state="loading" />);
    expect(screen.getAllByRole('status').length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText('正在载入内容').length).toBeGreaterThan(0);
  });
});

describe('UX-010 unknown enum handling', () => {
  it('shows an unknown support state with its raw value and records it', () => {
    clearUnknownStatusValues();
    const resolved = resolveStatus('support', 'TOTALLY_NEW_STATE');
    expect(resolved.unknown).toBe(true);
    expect(resolved.label).toBe('未知状态（TOTALLY_NEW_STATE）');
    expect(resolved.tone).toBe('muted');
    expect(unknownStatusValues()).toContain('support:TOTALLY_NEW_STATE');
  });

  it('does not treat an unknown state as FAILED or as success', () => {
    const resolved = resolveStatus('task', 'SOMETHING_ELSE');
    expect(resolved.label).not.toMatch(/失败|已完成/);
    expect(resolved.tone).toBe('muted');
  });

  it('labels a missing value as 未报告 rather than empty', () => {
    expect(resolveStatus('support', null).label).toBe('未报告');
    expect(resolveStatus('support', undefined).label).toBe('未报告');
  });

  it('keeps the frozen vocabulary intact for known values', () => {
    expect(resolveStatus('support', 'SUPPORTED').label).toBe('有证据支持');
    expect(resolveStatus('task', 'RUNNING').label).toBe('进行中');
    expect(resolveStatus('health', 'UNKNOWN').label).toBe('未测量');
    expect(resolveStatus('health', 'HEALTHY').tone).toBe('success');
  });
});
