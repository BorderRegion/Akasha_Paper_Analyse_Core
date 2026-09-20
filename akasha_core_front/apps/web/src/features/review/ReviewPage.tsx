/**
 * ReviewPage — the human review workbench (docs/03 §S06).
 *
 * Layout: queue on the left (short title, impact, error type), the claim with its
 * precise evidence in the middle, and run/model/verification records on the
 * right BEHIND an expander. One item is in focus at a time; the bottom bar holds
 * 已看过 / 仍有疑问 / 请求复核.
 *
 * Honesty rules implemented here:
 * - "已看过" records a user review decision and never changes support_state;
 * - a re-verification request says "复核任务已创建" and only claims success when
 *   the server accepted the work (202) — the scientific state refreshes later;
 * - the same request cannot be submitted twice: the button is disabled while in
 *   flight and the idempotency key is reused, so a retry cannot duplicate work;
 * - older runs and verification records stay visible next to the new ones;
 * - "no risky items" and "the audit service returned nothing" are different
 *   states.
 */

import { useMemo, useState, type ReactElement } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { StatusPill } from '../../components/ui/StatusPill';
import { errorCodeOf } from '../../lib/apiError';
import { claimTypeLabel } from '../../lib/status';
import { useSession } from '../../state/session';
import type { ReviewQueueItem } from '../../api/ui';
import { HandoffPanel } from '../handoff/HandoffPanel';
import styles from './review.module.css';

export const GROUP_LABELS: Record<ReviewQueueItem['group'], string> = {
  NUMERIC_OR_OCR: 'OCR/数字',
  EVIDENCE_UNSUPPORTED: '证据不支持',
  CONTRADICTION: '相互矛盾',
  SCOPE: '范围/协议',
  NOVELTY_UNVERIFIED: '未作外部新颖性验证',
  SUPPORT_STATE: '支持状态',
  NOT_VERIFIED: '未核查（审计未返回）',
};

export interface ReviewPageProps {
  workspaceId?: string;
}

export function ReviewPage({ workspaceId = 'local' }: ReviewPageProps): ReactElement {
  const { api, capabilities } = useSession();
  const queryClient = useQueryClient();
  const [includeSeen, setIncludeSeen] = useState(false);
  const [groups, setGroups] = useState<string[]>([]);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [showSources, setShowSources] = useState(false);
  const [decisionState, setDecisionState] = useState<{ code: string | null; message: string } | null>(
    null,
  );

  const reviewQuery = useQuery({
    queryKey: ['ui', 'review', { includeSeen, groups }] as const,
    queryFn: async () =>
      (
        await api.reviewQuery({
          include_seen: includeSeen,
          include_all: true,
          groups: groups.length ? groups : null,
          limit: 50,
        })
      ).data,
  });

  const items = reviewQuery.data?.items ?? [];
  const focus = useMemo(
    () => items.find((item) => item.claim.claim_id === focusId) ?? items[0] ?? null,
    [items, focusId],
  );

  const decision = useMutation({
    mutationFn: async (input: {
      claim: ReviewQueueItem;
      decision: 'SEEN' | 'NEEDS_REVIEW' | 'RESERVATION';
    }) =>
      (
        await api.reviewDecision({
          claim_id: input.claim.claim.claim_id,
          decision: input.decision,
          expected_claim_revision: input.claim.claim_revision,
          idempotency_key: `review:${input.claim.claim.claim_id}:${input.decision}`,
        })
      ).data,
    onSuccess: () => {
      setDecisionState({ code: null, message: '已记录你的审查意见（未修改论文的科学结论）' });
      void queryClient.invalidateQueries({ queryKey: ['ui', 'review'] });
    },
    onError: (error) => {
      setDecisionState({
        code: errorCodeOf(error),
        message: '审查意见未保存，未修改任何结论状态。',
      });
    },
  });

  const capability = capabilities?.capabilities.find((entry) => entry.code === 'review.queue');
  if (capability && capability.availability !== 'AVAILABLE') {
    return <AsyncBoundary state="capability_missing" capability="ui.review.queue" />;
  }

  const riskyCount = items.filter((item) => item.impact === 'HIGH').length;
  const unverifiedCount = items.filter((item) => item.group === 'NOT_VERIFIED').length;

  return (
    <section className={styles.page} aria-labelledby="review-heading">
      <header className={styles.header}>
        <h1 id="review-heading">待核查工作台</h1>
        <p className="muted" data-testid="queue-summary">
          当前队列 {items.length} 项（高影响 {riskyCount} 项，其中审计未返回 {unverifiedCount} 项）
        </p>
      </header>

      <div className={styles.filters}>
        <label>
          <input
            type="checkbox"
            checked={includeSeen}
            onChange={(event) => setIncludeSeen(event.target.checked)}
          />
          显示已看过的项
        </label>
        {(Object.keys(GROUP_LABELS) as ReviewQueueItem['group'][]).map((group) => (
          <button
            key={group}
            type="button"
            aria-pressed={groups.includes(group)}
            onClick={() =>
              setGroups((current) =>
                current.includes(group)
                  ? current.filter((entry) => entry !== group)
                  : [...current, group],
              )
            }
          >
            {GROUP_LABELS[group]}
          </button>
        ))}
      </div>

      <AsyncBoundary
        state={reviewQuery.isPending ? 'loading' : reviewQuery.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(reviewQuery.error)}
        scopeLabel="需要人工判断的结论"
        onRetry={() => void reviewQuery.refetch()}
      >
        {items.length === 0 && !reviewQuery.isPending ? (
          <p data-testid="review-empty">
            当前范围内没有需要人工判断的项。注意：这与“审计服务未返回”不同——可切换分组查看
            「未核查（审计未返回）」。
          </p>
        ) : null}

        <div className={styles.layout}>
          <ul className={styles.queue} aria-label="待核查项">
            {items.map((item) => (
              <li key={item.claim.claim_id}>
                <button
                  type="button"
                  aria-current={focus?.claim.claim_id === item.claim.claim_id}
                  onClick={() => setFocusId(item.claim.claim_id)}
                  data-group={item.group}
                >
                  <span className={styles.short}>{shorten(item.claim.statement)}</span>
                  <span className="muted">
                    {item.impact === 'HIGH' ? '高影响' : '一般'} · {GROUP_LABELS[item.group]}
                    {item.personal_decision ? ` · 已${decisionLabel(item.personal_decision)}` : ''}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          {focus ? (
            <article className={styles.detail} data-testid="review-detail">
              <h2>{focus.paper_title}</h2>
              <p className="muted">
                版本 {focus.paper_version_id} · {claimTypeLabel(focus.claim.claim_type)}
              </p>

              <div className={styles.claimHead}>
                <StatusPill family="support" value={focus.claim.support_state} />
                <span className="muted">{GROUP_LABELS[focus.group]}</span>
              </div>
              <p data-testid="review-statement">{focus.claim.statement}</p>

              <h3>原文与反证（并排可达）</h3>
              <div className={styles.columns}>
                <section data-testid="original-evidence">
                  <h4>本结论的证据</h4>
                  <ul>
                    {focus.original_evidence.flatMap((ref) => ref.evidence_ids).slice(0, 3).map((id) => (
                      <li key={id}>{id}</li>
                    ))}
                  </ul>
                  <Button
                    variant="quiet"
                    onClick={() => setShowSources(true)}
                    aria-expanded={showSources}
                  >
                    查看原文短引
                  </Button>
                </section>
                <section data-testid="counter-evidence">
                  <h4>反证 / 冲突来源</h4>
                  {focus.counter_evidence.length ? (
                    <ul>
                      {focus.counter_evidence
                        .flatMap((ref) => ref.evidence_ids)
                        .slice(0, 3)
                        .map((id) => (
                          <li key={id}>{id}</li>
                        ))}
                    </ul>
                  ) : (
                    <p className="muted">目前没有找到反证。</p>
                  )}
                </section>
              </div>

              <h3>核查原因</h3>
              <ul className={styles.reasons}>
                {focus.reasons.map((reason) => (
                  <li key={reason}>{reason}</li>
                ))}
              </ul>

              <Button
                variant="quiet"
                onClick={() => setShowSources((value) => !value)}
                aria-expanded={showSources}
                data-testid="toggle-sources"
              >
                分析来源（运行/模型/验证记录）
              </Button>
              {showSources ? (
                <div data-testid="verification-records">
                  <table className={styles.records}>
                    <caption className="muted">
                      历史记录全部保留：新的复核不会覆盖旧结论与旧运行。
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">验证器</th>
                        <th scope="col">结论</th>
                        <th scope="col">原因</th>
                        <th scope="col">run</th>
                      </tr>
                    </thead>
                    <tbody>
                      {focus.verifications.map((row) => (
                        <tr key={row.verification_id} data-verification-id={row.verification_id}>
                          <td>{row.verifier_type}</td>
                          <td>{row.verdict}</td>
                          <td>{row.reason_summary}</td>
                          <td>{row.run_id ?? '未报告'}</td>
                        </tr>
                      ))}
                      {!focus.verifications.length ? (
                        <tr>
                          <td colSpan={4}>审计服务未返回该结论的核查记录（与「核查通过」不同）</td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              ) : null}

              <ReviewDecisionBar
                item={focus}
                pending={decision.isPending}
                onDecide={(value) => decision.mutate({ claim: focus, decision: value })}
              />
              {decisionState ? (
                <p role="status" data-testid="decision-note">
                  {decisionState.message}
                  {decisionState.code ? `（${decisionState.code}）` : ''}
                </p>
              ) : null}
            </article>
          ) : null}
        </div>
      </AsyncBoundary>

      <HandoffPanel
        workspaceId={workspaceId}
        scope={items.map((item) => item.paper_version_id)}
      />
    </section>
  );
}

function shorten(statement: string): string {
  const firstSentence = statement.split(/[。.;；]/)[0] ?? statement;
  return firstSentence.length > 42 ? `${firstSentence.slice(0, 42)}…` : firstSentence;
}

function decisionLabel(decision: 'SEEN' | 'NEEDS_REVIEW' | 'RESERVATION'): string {
  return decision === 'SEEN' ? '看过' : decision === 'NEEDS_REVIEW' ? '仍有疑问' : '保留意见';
}

interface ReviewDecisionBarProps {
  item: ReviewQueueItem;
  pending: boolean;
  onDecide(decision: 'SEEN' | 'NEEDS_REVIEW' | 'RESERVATION'): void;
}

/**
 * The fixed bottom actions. "已看过" is a human decision recorded separately: it
 * never rewrites the claim's support_state (docs/06 §审查与外部Agent).
 */
export function ReviewDecisionBar({
  item,
  pending,
  onDecide,
}: ReviewDecisionBarProps): ReactElement {
  return (
    <div className={styles.decisionBar} role="group" aria-label="审查动作">
      {(
        [
          ['SEEN', '已看过'],
          ['NEEDS_REVIEW', '仍有疑问'],
          ['RESERVATION', '保留意见'],
        ] as const
      ).map(([value, label]) => (
        <Button
          key={value}
          variant={value === 'SEEN' ? 'primary' : 'quiet'}
          disabled={pending}
          onClick={() => onDecide(value)}
        >
          {label}
        </Button>
      ))}
      <ReverifyControl item={item} />
    </div>
  );
}

/**
 * Requesting a re-verification: pick the scope and the model role, submit once,
 * and report "task created" — never "verification finished".
 */
export function ReverifyControl({ item }: { item: ReviewQueueItem }): ReactElement {
  const { api } = useSession();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<'CURRENT' | 'VERSION_HIGH_RISK'>('CURRENT');
  const [role, setRole] = useState('verifier');
  const [result, setResult] = useState<{ state: string; operationId: string | null } | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    if (submitting) return; // a second click cannot create a second job
    setSubmitting(true);
    setErrorCode(null);
    try {
      const claimIds: string[] = [];
      if (scope === 'CURRENT') {
        claimIds.push(item.claim.claim_id);
      } else {
        let offset = 0;
        for (;;) {
          const page = (await api.reviewQuery({
            paper_version_id: item.claim.paper_version_id,
            include_seen: true, include_all: false, limit: 100, offset,
          })).data;
          claimIds.push(...page.items.filter((entry) => entry.impact === 'HIGH').map((entry) => entry.claim.claim_id));
          if (!page.has_more) break;
          if (!page.items.length) throw new Error('Review pagination did not advance');
          offset += page.items.length;
        }
        if (!claimIds.length) throw new Error('No high risk claims in this version');
      }
      const response = await api.submitOperation({
        kind: 'reverify',
        payload: { claim_ids: claimIds, model_role: role, scope },
        // One key per (scope, role, claim set): a retry of the SAME request
        // cannot enqueue duplicate work.
        idempotency_key: `reverify:${scope}:${role}:${claimIds.join(',')}`,
      });
      setResult({ state: response.data.state, operationId: response.data.operation_id });
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ['ui', 'review'] });
    } catch (error) {
      setErrorCode(errorCodeOf(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <span className={styles.reverify}>
      <Button
        variant="quiet"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        disabled={submitting}
      >
        请求复核
      </Button>
      {open ? (
        <div role="dialog" aria-label="请求复核" data-testid="reverify-dialog">
          <fieldset>
            <legend>范围</legend>
            <label>
              <input
                type="radio"
                name="scope"
                checked={scope === 'CURRENT'}
                onChange={() => setScope('CURRENT')}
              />
              当前结论（1 项）
            </label>
            <label>
              <input
                type="radio"
                name="scope"
                checked={scope === 'VERSION_HIGH_RISK'}
                onChange={() => setScope('VERSION_HIGH_RISK')}
              />
              该版本高风险项
            </label>
          </fieldset>
          <label>
            模型角色
            <select value={role} onChange={(event) => setRole(event.target.value)}>
              <option value="verifier">verifier</option>
              <option value="analyst">analyst</option>
            </select>
          </label>
          <p className="muted" data-testid="reverify-estimate">
            预计任务数：{scope === 'CURRENT' ? 1 : '未知（高风险项数量由服务器统计）'} · 资源档位：
            未知（以服务器 triage 结果为准）
          </p>
          <Button onClick={() => void submit()} disabled={submitting}>
            {submitting ? '提交中…' : '提交复核'}
          </Button>
          <Button variant="quiet" onClick={() => setOpen(false)}>
            取消
          </Button>
        </div>
      ) : null}
      {result ? (
        <span role="status" data-testid="reverify-result">
          复核任务已创建（operation {result.operationId}，state={result.state}）。科学结论状态将在任务真正完成后刷新。
        </span>
      ) : null}
      {errorCode ? (
        <span role="alert" data-testid="reverify-error">
          复核任务未创建（{errorCode}），未改变任何结论。
        </span>
      ) : null}
    </span>
  );
}
