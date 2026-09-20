import type { ReactElement } from 'react';
import { resolveStatus, type StatusFamily } from '../../lib/status';
import styles from './ui.module.css';

export interface StatusPillProps {
  family: StatusFamily;
  value: string | null | undefined;
  /** Optional human prefix, e.g. "分析". */
  prefix?: string;
}

/**
 * The single place a status word is rendered. An unknown server enum is shown
 * with its raw value and marked, never coerced into a known state.
 */
export function StatusPill({ family, value, prefix }: StatusPillProps): ReactElement {
  const status = resolveStatus(family, value);
  return (
    <span
      className={styles.statusPill}
      data-tone={status.tone}
      data-unknown={status.unknown ? 'true' : undefined}
      title={status.unknown ? `未在冻结词表中：${status.raw}` : undefined}
    >
      {prefix ? <span className={styles.muted}>{prefix}</span> : null}
      {status.label}
    </span>
  );
}
