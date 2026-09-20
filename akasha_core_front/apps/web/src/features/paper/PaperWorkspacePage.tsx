/**
 * PaperWorkspacePage (docs/03 §S04 + docs/07).
 *
 * The version is PINNED by the URL: opening a paper fixes
 * `paper_id + paper_version_id`, and switching versions is an explicit act that
 * reloads that version — a newer version is announced, never jumped to. Evidence
 * requested for a claim is validated against the pinned version/hash before it
 * can be drawn (docs/07 §1).
 */

import { useCallback, useEffect, useMemo, type ReactElement } from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import type { ClaimPreview, ModuleView, Workspace } from '../../api/contract';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { StatusPill } from '../../components/ui/StatusPill';
import { Distribution } from '../../components/ui/Distribution';
import { errorCodeOf } from '../../lib/apiError';
import { claimTypeLabel, resolveStatus, tierLabel } from '../../lib/status';
import { usePersonalMutation, useWorkspace } from '../../state/queries';
import { NotesTab } from './NotesTab';
import { ReaderPanel } from '../reader/ReaderPanel';
import styles from './paper.module.css';

type TabId = 'overview' | 'method' | 'experiments' | 'notes' | 'audit';

const TABS: Array<{ id: TabId; label: string; moduleId: string | null }> = [
  { id: 'overview', label: '概览', moduleId: 'overview' },
  { id: 'method', label: '方法', moduleId: 'method' },
  { id: 'experiments', label: '实验', moduleId: 'experiments' },
  { id: 'notes', label: '笔记', moduleId: null },
  { id: 'audit', label: '核查', moduleId: 'audit' },
];

export interface PaperWorkspacePageProps {
  /** Test seam: inject the PDF engine factory. */
  createEngine?: Parameters<typeof ReaderPanel>[0]['createEngine'];
}

export function PaperWorkspacePage({ createEngine }: PaperWorkspacePageProps): ReactElement {
  const { paperId = '' } = useParams();
  const [params, setParams] = useSearchParams();
  const pinnedVersion = params.get('paper_version_id');
  const tab = TABS.some(entry => entry.id === params.get('tab')) ? params.get('tab') as TabId : 'overview';
  // The selected claim and the reader live in the URL: state survives the
  // pinning navigation, a link to "this claim's evidence" works, and the back
  // button steps out of the inspector the way a user expects.
  const selectedClaimId = params.get('claim');
  const readerOpen = params.get('reader') === '1' || selectedClaimId !== null;

  const workspace = useWorkspace(paperId, pinnedVersion);
  const personal = usePersonalMutation(paperId);

  // The version is pinned by the server's response and then held by the client.
  // It is written into the URL with history.replaceState (NOT a router
  // navigation): a router navigation here would remount the page and throw away
  // the tab/inspector state the user is in the middle of.
  const selectedVersion = workspace.data?.selected_version_id ?? null;
  const effectiveVersion = pinnedVersion ?? selectedVersion;
  const rawPage = Number(params.get('page'));
  const readerPage = Number.isInteger(rawPage) && rawPage > 0 ? rawPage : 1;
  const updatePage = useCallback((page: number) => {
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (effectiveVersion) next.set('paper_version_id', effectiveVersion);
      next.set('page', String(page));
      return next;
    }, { replace: true });
  }, [setParams, effectiveVersion]);

  useEffect(() => {
    if (!effectiveVersion) return;
    if (new URLSearchParams(window.location.search).get('paper_version_id') === effectiveVersion) {
      return;
    }
    const next = new URLSearchParams(window.location.search);
    next.set('paper_version_id', effectiveVersion);
    window.history.replaceState(window.history.state, '', `${window.location.pathname}?${next.toString()}`);
  }, [effectiveVersion]);

  const newerVersion = useMemo(() => {
    const versions = workspace.data?.versions ?? [];
    if (!effectiveVersion || versions.length < 2) return null;
    const latest = versions[0];
    return latest && latest.id !== effectiveVersion ? latest : null;
  }, [effectiveVersion, workspace.data?.versions]);

  const activeModule = TABS.find((entry) => entry.id === tab)?.moduleId ?? null;
  const module = workspace.data?.modules.find((entry) => entry.id === activeModule) ?? null;
  const allClaims = useMemo(
    () => (workspace.data?.modules ?? []).flatMap((entry) => entry.claims),
    [workspace.data?.modules],
  );
  const inspectorClaim = useMemo(
    () => allClaims.find((claim) => claim.claim_id === selectedClaimId) ?? null,
    [allClaims, selectedClaimId],
  );

  const setUrlState = (mutate: (next: URLSearchParams) => void) => {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      // The pinned version is part of every navigation this page performs, so
      // the URL can never lose it (the pin itself was written with
      // replaceState, which the router does not track).
      if (effectiveVersion) next.set('paper_version_id', effectiveVersion);
      mutate(next);
      return next;
    });
  };
  const openEvidence = (claim: ClaimPreview) => {
    setUrlState((next) => {
      next.set('claim', claim.claim_id);
      next.set('reader', '1');
    });
  };
  const closeInspector = () => {
    setUrlState((next) => {
      next.delete('claim');
    });
    const target = [...document.querySelectorAll<HTMLElement>('[data-claim-id]')]
      .find(element => element.dataset.claimId === selectedClaimId)?.querySelector<HTMLButtonElement>('button');
    target?.focus({ preventScroll: true });
    target?.scrollIntoView?.({ block: 'nearest' });
  };

  if (workspace.isPending) {
    return (
      <section className={styles.page}>
        <AsyncBoundary state="loading" />
      </section>
    );
  }
  if (workspace.isError || !workspace.data) {
    return (
      <section className={styles.page}>
        <AsyncBoundary
          state="error"
          errorCode={errorCodeOf(workspace.error)}
          onRetry={() => void workspace.refetch()}
        />
      </section>
    );
  }

  const data: Workspace = workspace.data;

  return (
    <section className={styles.page} aria-labelledby="paper-heading">
      <header className={styles.header}>
        <div>
          <h1 id="paper-heading">{data.paper.title}</h1>
          <p className="muted">
            {data.paper.authors.value?.join(', ') ?? '作者待补充'} ·{' '}
            {data.paper.year.value ?? '年份待补充'} · {data.paper.venue.value ?? '来源待补充'}
          </p>
          <details className="muted" data-testid="pinned-version">
            <summary>版本信息{tierLabel(data.paper.tier) ? ` · ${tierLabel(data.paper.tier)}` : ''}</summary>
            <p>当前版本：{effectiveVersion}</p>
          </details>
        </div>
        <div className={styles.headerActions}>
          <Button
            variant="quiet"
            aria-pressed={data.paper.personal.saved}
            onClick={() =>
              personal.mutate({
                saved: !data.paper.personal.saved,
                expected_revision: data.paper.personal.revision,
              })
            }
          >
            {data.paper.personal.saved ? '已收藏' : '收藏'}
          </Button>
          <Button
            variant="quiet"
            aria-pressed={readerOpen}
            onClick={() =>
              setUrlState((next) => {
                if (readerOpen) {
                  next.delete('reader');
                  next.delete('claim');
                } else {
                  next.set('reader', '1');
                }
              })
            }
          >
            {readerOpen ? '收起原文' : '原文'}
          </Button>
          <Link to={`/app/collections?paper_id=${encodeURIComponent(paperId)}`} className="muted">
            加入专题
          </Link>
        </div>
      </header>

      {newerVersion ? (
        <p role="status" data-testid="newer-version-notice">
          该论文有更新的版本 {newerVersion.id}
          <Button
            variant="quiet"
            onClick={() =>
              setUrlState((next) => {
                next.set('paper_version_id', newerVersion.id);
                next.delete('page');
              })
            }
          >
            切换到新版本
          </Button>
          <span className="muted">原有笔记和核查记录会留在旧版本。</span>
        </p>
      ) : null}

      <div className={styles.tabs} role="tablist" aria-label="论文页签">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            role="tab"
            type="button"
            aria-selected={tab === entry.id}
            onClick={() => setUrlState(next => next.set('tab', entry.id))}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div className={styles.body} data-reader-open={readerOpen ? 'true' : 'false'}>
        <div className={styles.analysis}>
          {tab === 'audit' ? <div data-testid="audit-summary">
            <Distribution label="证据支持分布" items={Object.entries(data.paper.audit.by_state).map(([state, count]) => ({ key: state, label: resolveStatus('support', state).label, count, tone: resolveStatus('support', state).tone }))} />
          </div> : null}
          {tab === 'notes' ? (
            <NotesTab
              key={`${paperId}:${effectiveVersion}`}
              paperId={paperId}
              paperVersionId={effectiveVersion ?? ''}
              revision={data.paper.personal.revision}
            />
          ) : (
            <ModuleTab
              module={module}
              moduleId={activeModule}
              onOpenEvidence={openEvidence}
              inspectorClaim={inspectorClaim}
            />
          )}
        </div>

        {readerOpen ? (
          <ReaderPanel
            key={effectiveVersion}
            initialPage={readerPage}
            onPageChange={updatePage}
            onRememberPage={page => personal.mutate({ reading_anchor: { paper_version_id: effectiveVersion ?? '', page_number: page }, read_state: data.paper.personal.read_state === 'READ' ? 'READ' : 'READING', expected_revision: data.paper.personal.revision })}
            remembering={personal.isPending}
            paperVersionId={effectiveVersion ?? ''}
            documentSha256={data.document_sha256}
            inspectorClaim={inspectorClaim}
            onCloseInspector={closeInspector}
            createEngine={createEngine}
            onBackToClaims={closeInspector}
          />
        ) : null}
      </div>
      {personal.isError ? <p role="alert">保存失败（{errorCodeOf(personal.error)}），请重试。</p> : null}
      {personal.isSuccess && personal.variables?.reading_anchor ? <p role="status">已记住这一页，下次可以从桌面继续阅读。</p> : null}
    </section>
  );
}

interface ModuleTabProps {
  module: ModuleView | null;
  moduleId: string | null;
  inspectorClaim: ClaimPreview | null;
  onOpenEvidence(claim: ClaimPreview): void;
}

function ModuleTab({ module, moduleId, onOpenEvidence, inspectorClaim }: ModuleTabProps): ReactElement {
  if (!module) {
    return (
      <AsyncBoundary state="capability_missing" capability={`ui.paper.${moduleId ?? 'workspace'}`} />
    );
  }
  if (module.availability !== 'AVAILABLE') {
    return (
      <AsyncBoundary state="capability_missing" capability={`ui.paper.${module.id}`}>
        <p className="muted">{module.reason}</p>
      </AsyncBoundary>
    );
  }
  return (
    <div className={styles.module} data-module={module.id}>
      {moduleId === 'method' ? <MethodFlow claims={module.claims} /> : null}
      {moduleId === 'overview' || moduleId === 'audit' ? (
        <ClaimList
          claims={module.claims}
          onOpenEvidence={onOpenEvidence}
          selectedClaimId={inspectorClaim?.claim_id ?? null}
        />
      ) : null}
      {moduleId === 'experiments' ? (
        <ResultsSection claims={module.claims} onOpenEvidence={onOpenEvidence} />
      ) : null}
    </div>
  );
}

/**
 * Overview / audit claim list. Every line is bound to a STORED claim id: the UI
 * never writes a summary of its own (docs/03 §S04, UX-025).
 */
export function ClaimList({
  claims,
  onOpenEvidence,
  selectedClaimId,
}: {
  claims: ClaimPreview[];
  onOpenEvidence(claim: ClaimPreview): void;
  selectedClaimId: string | null;
}): ReactElement {
  if (!claims.length) {
    return <p className="muted">该版本没有已保存的结论（未提取）。</p>;
  }
  return (
    <ul className={styles.claims}>
      {claims.map((claim) => (
        <li
          key={claim.claim_id}
          className={styles.claim}
          data-claim-id={claim.claim_id}
          data-selected={claim.claim_id === selectedClaimId}
        >
          <div className={styles.claimHead}>
            <StatusPill family="support" value={claim.support_state} />
            <span className="muted">{claimTypeLabel(claim.claim_type)}</span>
            <button
              type="button"
              className={styles.evidenceButton}
              onClick={() => onOpenEvidence(claim)}
            >
              证据（{claim.evidence_count}）
            </button>
          </div>
          <p className={styles.claimText}>{claim.statement}</p>
        </li>
      ))}
    </ul>
  );
}

/**
 * Method flow (UX-026): the reader page shows only structure the server already
 * stored. There is no graph editor and no invention: the stored method claims
 * are rendered as an ordered, numbered sequence, and the UI says plainly that it
 * is a step list rather than pretending to a diagram the backend did not emit.
 */
export function MethodFlow({ claims }: { claims: ClaimPreview[] }): ReactElement {
  if (!claims.length) {
    return <p className="muted">该方法版本没有已保存的结构化步骤。</p>;
  }
  return (
    <div data-testid="method-flow">
      <p className="muted" data-testid="method-degraded">
        以下按记录顺序列出方法步骤，步骤之间的依赖关系尚未整理。
      </p>
      <ol className={styles.steps}>
        {claims.map((claim) => (
          <li key={claim.claim_id} data-claim-id={claim.claim_id}>
            {claim.statement}
          </li>
        ))}
      </ol>
    </div>
  );
}

/**
 * Results/units section (UX-027).
 *
 * The backend stores claim statements as TEXT and exposes no structured
 * value/unit cell, so this view renders each stored statement VERBATIM: numbers
 * keep their precision, "个百分点" is never rewritten as "%", footnote markers
 * stay attached, and anything the backend did not provide is shown as
 * "未报告"/"未结构化" instead of 0. Parsing numbers out of prose would invent
 * precision the system does not have (docs/07 §4).
 */
export function ResultsSection({
  claims,
  onOpenEvidence,
}: {
  claims: ClaimPreview[];
  onOpenEvidence(claim: ClaimPreview): void;
}): ReactElement {
  const footnotes = collectFootnotes(claims.map((claim) => claim.statement));
  return (
    <div data-testid="results-section">
      <table className={styles.results}>
        <caption className="muted">
          保留记录中的数值与精度。单位和误差尚未单独整理，可点开证据对照原文。
        </caption>
        <thead>
          <tr>
            <th scope="col">结论（原文）</th>
            <th scope="col">单位</th>
            <th scope="col">误差/方差</th>
            <th scope="col">证据</th>
          </tr>
        </thead>
        <tbody>
          {claims.map((claim) => (
            <tr key={claim.claim_id} data-claim-id={claim.claim_id}>
              <td>{claim.statement}</td>
              <td data-testid="unit-cell">未结构化</td>
              <td data-testid="variance-cell">未报告</td>
              <td>
                <button type="button" onClick={() => onOpenEvidence(claim)}>
                  {claim.evidence_count} 条证据
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {footnotes.length ? (
        <ul className={styles.footnotes} data-testid="footnotes">
          {footnotes.map((marker) => (
            <li key={marker}>
              {marker}：原文脚注标记，含义以原文为准
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** Footnote markers that appear in the stored text, preserved verbatim. */
export function collectFootnotes(statements: string[]): string[] {
  const markers = new Set<string>();
  for (const statement of statements) {
    for (const match of statement.matchAll(/(?:\[\d+\]|[†‡*§]|\b\d+\))/g)) {
      markers.add(match[0]);
    }
  }
  return [...markers];
}
