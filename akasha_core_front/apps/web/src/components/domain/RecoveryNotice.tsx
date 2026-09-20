/**
 * RecoveryNotice — the documented recovery path for every HTTP failure
 * (docs/05 §异常文案, UX-052).
 *
 * Each status has ONE clear next step, and nothing is ever silently turned into
 * an empty result:
 * - 401 → sign in again (the session expired);
 * - 403 → the scope refuses this action; re-scope or reload;
 * - 409 → both copies are kept; reload and merge;
 * - 412 → the record changed; reload it;
 * - 422 → the request fields are wrong (a contract bug, not user data);
 * - 429 → rate limited; wait and retry the SAME request;
 * - 5xx → service failure; the previous content stays visible and can be retried.
 */

import type { ReactElement } from 'react';
import { Button } from '../ui/Button';

export interface RecoveryNoticeProps {
  status: number | null;
  code?: string | null;
  message?: string | null;
  onRetry?(): void;
  onSignIn?(): void;
  /** Old content is still on screen (stale/partial). */
  preserveContent?: boolean;
}

interface RecoveryPlan {
  title: string;
  action: string;
  retryable: boolean;
}

export function recoveryPlan(status: number | null, code?: string | null): RecoveryPlan {
  if (code === 'RESET_CURSOR' || code === 'SCOPE_EXPIRED') {
    return { title: '结果范围已变化', action: '从第一页重新载入（保留筛选）', retryable: true };
  }
  if (code === 'REVISION_CONFLICT') {
    return { title: '内容已被其他人修改', action: '保留两份内容并合并', retryable: false };
  }
  switch (status) {
    case 401:
      return { title: '登录状态已失效', action: '重新登录后继续', retryable: false };
    case 403:
      return { title: '当前范围不允许该操作', action: '检查范围或返回列表重试', retryable: false };
    case 404:
      return { title: '对象不存在或已删除', action: '返回列表确认最新状态', retryable: false };
    case 409:
      return { title: '与服务端状态冲突', action: '重新载入后再提交', retryable: true };
    case 412:
      return { title: '记录已更新', action: '重新载入该记录', retryable: true };
    case 413:
      return { title: '内容超出体积上限', action: '缩小范围或提高上限', retryable: false };
    case 422:
      return {
        title: '请求字段不符合契约',
        action: '契约不匹配，请导出诊断而不是重试',
        retryable: false,
      };
    case 429:
      return { title: '请求过于频繁', action: '稍后重试同一个请求', retryable: true };
    default:
      if (status !== null && status >= 500) {
        return { title: '服务暂时不可用', action: '保留当前内容并重试', retryable: true };
      }
      if (status === null) {
        return { title: '连接中断', action: '仍保留上次结果，恢复后重试', retryable: true };
      }
      return { title: '请求失败', action: '重试', retryable: true };
  }
}

export function RecoveryNotice({
  status,
  code,
  message,
  onRetry,
  onSignIn,
  preserveContent = false,
}: RecoveryNoticeProps): ReactElement {
  const plan = recoveryPlan(status, code);
  return (
    <div role="alert" data-testid="recovery-notice" data-status={status ?? 'offline'}>
      <p data-testid="recovery-title">{plan.title}</p>
      <p className="muted" data-testid="recovery-action">
        {plan.action}
        {code ? `（错误码：${code}）` : ''}
        {preserveContent ? ' · 已保留上次结果' : ''}
      </p>
      {message ? <p className="muted">{message}</p> : null}
      <div>
        {status === 401 && onSignIn ? <Button onClick={onSignIn}>去登录</Button> : null}
        {plan.retryable && onRetry ? <Button onClick={onRetry}>重试</Button> : null}
      </div>
    </div>
  );
}
