import type { ReactElement, ReactNode } from 'react';
import { Button } from '../ui/Button';
import { Skeleton } from '../ui/Skeleton';

/**
 * The single data-state contract (spec docs/05 §通用数据状态):
 * loading / ready / empty / partial / stale / error / forbidden /
 * capability_missing. An error never clears already-read content and is never
 * converted into an empty list.
 */
export type DataState =
  | 'loading'
  | 'ready'
  | 'empty'
  | 'partial'
  | 'stale'
  | 'error'
  | 'forbidden'
  | 'capability_missing';

export interface AsyncBoundaryProps {
  state: DataState;
  /** Human reason code from the server (never invented by the UI). */
  errorCode?: string | null;
  /** Scope description for the empty state ("当前范围：本专题"). */
  scopeLabel?: string;
  /** Which capability is missing (capability_missing). */
  capability?: string | null;
  missingReason?: string | null;
  lastUpdatedAt?: string | null;
  onRetry?: () => void;
  /** Content kept visible while stale / partial / error. */
  children?: ReactNode;
}

export function AsyncBoundary(props: AsyncBoundaryProps): ReactElement {
  const { state, errorCode, scopeLabel, capability, lastUpdatedAt, onRetry, children } = props;

  if (state === 'loading') {
    return (
      <div data-state="loading">
        <Skeleton height={20} width="40%" label="正在载入内容" />
        <Skeleton height={14} width="90%" label="正在载入内容" />
        <Skeleton height={14} width="75%" label="正在载入内容" />
      </div>
    );
  }

  if (state === 'forbidden') {
    return (
      <div role="alert" data-state="forbidden">
        <p>需要登录，或当前账户没有访问权限。</p>
        {onRetry ? <Button onClick={onRetry}>去登录</Button> : null}
      </div>
    );
  }

  if (state === 'capability_missing') {
    return (
      <div role="status" data-state="capability_missing">
        <p>
          这部分内容暂时不可用。
        </p>
        {/* The server's own reason (when it has one) is the honest explanation;
            hiding it would turn "not analysed yet" into "not implemented". */}
        {children}
        {capability ? <details><summary>查看详情</summary><p className="muted">功能：{capability}</p></details> : null}
      </div>
    );
  }

  if (state === 'error' && !children) {
    return (
      <div role="alert" data-state="error">
        <p>请求失败，已保留页面状态。{errorCode ? `错误码：${errorCode}` : ''}</p>
        {onRetry ? <Button onClick={onRetry}>重试</Button> : null}
      </div>
    );
  }

  return (
    <div data-state={state}>
      {state === 'stale' ? (
        <p role="status" className="muted">
          连接中断或数据未更新，仍显示上次结果（{lastUpdatedAt ?? '未知时间'}）。
          {onRetry ? <Button variant="quiet" onClick={onRetry}>刷新</Button> : null}
        </p>
      ) : null}
      {state === 'partial' ? (
        <p role="status" className="muted">
          部分内容加载失败，以下为已成功部分。
          {onRetry ? <Button variant="quiet" onClick={onRetry}>重试失败部分</Button> : null}
        </p>
      ) : null}
      {state === 'empty' ? (
        <div data-state="empty">
          <p>当前范围内没有内容{scopeLabel ? `（${scopeLabel}）` : ''}。</p>
        </div>
      ) : null}
      {state === 'error' && children ? (
        <p role="alert">
          刷新失败，已保留上次结果。{errorCode ? `错误码：${errorCode}` : ''}
          {onRetry ? <Button variant="quiet" onClick={onRetry}>重试</Button> : null}
        </p>
      ) : null}
      {children}
    </div>
  );
}
