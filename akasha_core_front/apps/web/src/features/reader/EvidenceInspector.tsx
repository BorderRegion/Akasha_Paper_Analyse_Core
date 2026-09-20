/**
 * EvidenceInspector (docs/05 + docs/07 §5).
 *
 * Shows the original text, the claim and a SHORT verification summary by
 * default; run/model/prompt details appear only when "分析来源" is expanded. The
 * original language is kept verbatim and Chinese explanation is a separate
 * label — translation never replaces the quote. An uncalibrated model
 * self-report is never presented as a probability of truth.
 */

import { useState, type ReactElement } from 'react';
import type { ClaimPreview, EvidenceLocator, PageEvidence } from '../../api/contract';
import { Button } from '../../components/ui/Button';
import { StatusPill } from '../../components/ui/StatusPill';
import { claimTypeLabel } from '../../lib/status';
import styles from './reader.module.css';

/** The claim's own evidence row from `GET /v1/claims/{id}/evidence`. */
export interface ClaimEvidenceRow {
  evidence_id: string;
  paper_version_id?: string;
  page_start?: number;
  text?: string;
  source_method?: string;
  ocr_confidence?: number | null;
  content_sha256?: string;
  extraction_run_id?: string | null;
  quality_state?: string;
}

export interface EvidenceInspectorProps {
  claim: ClaimPreview;
  locator: EvidenceLocator | null;
  pageEvidence: PageEvidence | null;
  /** The evidence row behind the locator, when the claim names it. */
  evidenceRow?: ClaimEvidenceRow | null;
  onBackToClaims(): void;
}

export function EvidenceInspector({
  claim,
  locator,
  pageEvidence,
  evidenceRow = null,
  onBackToClaims,
}: EvidenceInspectorProps): ReactElement {
  const [showSources, setShowSources] = useState(false);
  const hashMatches =
    locator !== null &&
    pageEvidence !== null &&
    locator.document_sha256 === pageEvidence.document_sha256 &&
    locator.paper_version_id === pageEvidence.paper_version_id;

  return (
    <section aria-label="证据详情" className={styles.inspector} data-testid="evidence-inspector">
      <header className={styles.inspectorHead}>
        <StatusPill family="support" value={claim.support_state} />
        <span className="muted">{claimTypeLabel(claim.claim_type)}</span>
        <Button variant="quiet" onClick={onBackToClaims}>
          返回结论
        </Button>
      </header>

      <h3>结论</h3>
      <p data-testid="inspector-claim">{claim.statement}</p>

      <h3>原文（原语言）</h3>
      {locator ? (
        hashMatches ? (
          evidenceRow?.text ? (
            // Verbatim: no rounding, no translation, no paraphrase.
            <blockquote data-testid="inspector-quote">{evidenceRow.text}</blockquote>
          ) : (
            <p className="muted" data-testid="inspector-quote">
              这条证据暂时没有可显示的原文片段。
            </p>
          )
        ) : (
          <p role="alert" className="muted" data-testid="inspector-quote">
            这条证据与当前文件不匹配，暂不显示引用。
          </p>
        )
      ) : (
        <p className="muted">尚未选择证据。</p>
      )}

      <h3>定位</h3>
      <p className="muted" data-testid="inspector-precision">
        {locator
          ? locator.precision === 'REGION'
            ? `可定位区域（第 ${locator.page_number} 页）`
            : `${locator.precision === 'PAGE' ? '只能定位到页面' : '仅有文本'}（第 ${locator.page_number} 页）${
                locator.reason ? `，原因：${locator.reason}` : ''
              }`
          : '未定位'}
      </p>

      <h3>核查摘要</h3>
      <p className="muted">
        共 {claim.evidence_count} 条证据。是否足以支持结论，还需要结合原文判断。
      </p>

      <Button variant="quiet" onClick={() => setShowSources((value) => !value)} aria-expanded={showSources}>
        分析来源
      </Button>
      {showSources ? (
        <dl className={styles.sources} data-testid="analysis-sources">
          <dt>extraction_run_id</dt>
          <dd>{locator?.extraction_run_id ?? '未报告'}</dd>
          <dt>transform_revision</dt>
          <dd>{locator?.transform_revision ?? '未报告'}</dd>
          <dt>source_method</dt>
          <dd>{locator?.source_method ?? '未报告'}</dd>
          <dt>ocr_confidence</dt>
          <dd>{locator?.ocr_confidence ?? '未报告'}</dd>
          <dt>quality_state</dt>
          <dd>{evidenceRow?.quality_state ?? '未报告'}</dd>
          <dt>page_start</dt>
          <dd>{evidenceRow?.page_start ?? '未报告'}</dd>
        </dl>
      ) : null}
    </section>
  );
}
