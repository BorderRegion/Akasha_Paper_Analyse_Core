/**
 * PaperRow — one literature row (docs/05: "一篇文献短信息与选择").
 *
 * It never fetches on its own and never invents values: a missing author, year
 * or venue is rendered through the missing-value labels, and a claim hit is not
 * accepted here at all (the library renders claims with a different component).
 * Row height stays stable whether or not optional data exists.
 */

import type { ReactElement } from 'react';
import { Link } from 'react-router-dom';
import type { PaperListItem } from '../../api/contract';
import type { PaperRowPayload } from '../../api/ui';
import { formatNumber } from '../../lib/format';
import { readStateLabel, tierLabel } from '../../lib/status';
import { StatusPill } from '../../components/ui/StatusPill';
import { Tag } from '../../components/ui/Tag';
import styles from './library.module.css';

export interface PaperRowProps {
  paper: PaperRowPayload | PaperListItem;
  selected: boolean;
  onToggleSelected(paperId: string): void;
  onOpenSaved?(paperId: string): void;
  onToggleSaved?(paperId: string, saved: boolean): void;
  savedPending?: boolean;
  active?: boolean;
}

function fact(value: { value?: unknown; missing_reason?: string | null } | undefined): string {
  if (!value || value.value === null || value.value === undefined) {
    return value?.missing_reason ? `—（${value.missing_reason}）` : `—（未知）`;
  }
  if (Array.isArray(value.value)) return value.value.join(', ');
  return String(value.value);
}

export function PaperRow(props: PaperRowProps): ReactElement {
  const { paper, selected, onToggleSelected, onToggleSaved, savedPending } = props;
  const authors = fact(paper.authors as never);
  const year = paper.year?.value ?? null;
  const venue = fact(paper.venue as never);

  return (
    <li className={styles.row} data-selected={selected} data-paper-id={paper.paper_id}>
      <label className={styles.check}>
        <input
          type="checkbox"
          checked={selected}
          onChange={() => onToggleSelected(paper.paper_id)}
          aria-label={`选择 ${paper.title}`}
        />
      </label>

      <div className={styles.body}>
        <Link className={styles.title} to={`/app/papers/${paper.paper_id}`}>
          {paper.title}
        </Link>
        {paper.takeaway ? (
          <p className={styles.takeaway}>{paper.takeaway.statement}</p>
        ) : (
          <p className={`${styles.takeaway} muted`}>尚无已保存的要点（未提取）</p>
        )}
        <p className={styles.meta}>
          <span>{authors}</span>
          <span aria-label="年份">{formatNumber(year as number | null, 'NOT_REPORTED')}</span>
          <span>{venue}</span>
        </p>
        <div className={styles.tags}>
          {paper.tags.slice(0, 3).map((tag) => (
            <Tag key={tag.id} label={tag.label} namespace={tag.namespace} candidate={tag.is_candidate} />
          ))}
          {tierLabel(paper.tier) ? <Tag label={tierLabel(paper.tier) as string} namespace="tier" /> : null}
        </div>
      </div>

      <div className={styles.status}>
        <StatusPill family="support" value={paper.audit.total ? dominantState(paper.audit.by_state) : null} />
        <span className="muted">{paper.audit.total} 条结论</span>
        <span className="muted">{readStateLabel(paper.personal.read_state)}</span>
      </div>

      <div className={styles.actions}>
        <button
          type="button"
          className={styles.star}
          aria-pressed={paper.personal.saved}
          aria-label={paper.personal.saved ? '取消收藏' : '收藏'}
          disabled={savedPending}
          onClick={() => onToggleSaved?.(paper.paper_id, !paper.personal.saved)}
        >
          {paper.personal.saved ? '★' : '☆'}
        </button>
        <span className="muted" data-testid="saved-state">
          {paper.personal.saved ? '已收藏' : '未收藏'}
        </span>
      </div>
    </li>
  );
}

function dominantState(byState: Record<string, number>): string | null {
  const entries = Object.entries(byState);
  if (!entries.length) return null;
  entries.sort((a, b) => b[1] - a[1]);
  return entries[0]?.[0] ?? null;
}
