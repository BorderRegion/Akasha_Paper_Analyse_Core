import type { ReactElement } from 'react';
import { claimTypeLabel } from '../../lib/status';
import { StatusPill } from '../ui/StatusPill';

export interface EvidenceBadgeProps {
  claimType: string;
  supportState: string;
  evidenceCount: number;
  onOpen?: () => void;
}

/**
 * Claim nature + evidence entry (spec docs/05). Support is a state word, not a
 * probability; the evidence count is shown as a count, never as a score.
 */
export function EvidenceBadge({
  claimType,
  supportState,
  evidenceCount,
  onOpen,
}: EvidenceBadgeProps): ReactElement {
  return (
    <span className="row">
      <span className="muted">{claimTypeLabel(claimType)}</span>
      <StatusPill family="support" value={supportState} />
      <button type="button" className="linkLike" onClick={onOpen} disabled={!onOpen}>
        证据 {evidenceCount > 0 ? `(${evidenceCount})` : '（未报告）'}
      </button>
    </span>
  );
}
