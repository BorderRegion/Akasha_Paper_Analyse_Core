import type { ReactElement } from 'react';
import { Link } from 'react-router-dom';
import styles from '../shell/shell.module.css';

export interface StatusBarProps {
  processing?: number;
  needsAttention?: number;
}

/**
 * The only always-on status surface (docs/02): a short sentence, not a metrics
 * wall. With nothing to report it stays quiet.
 */
export function StatusBar({ processing = 0, needsAttention = 0 }: StatusBarProps): ReactElement {
  const parts: string[] = [];
  if (processing > 0) parts.push(`${processing} 项处理中`);
  if (needsAttention > 0) parts.push(`${needsAttention} 项需处理`);
  return (
    <footer className={styles.statusBar} role="contentinfo" aria-live="polite">
      {parts.length > 0 ? (
        <span>
          {parts.join(' · ')} <Link to="/app/operations">查看运行状态</Link>
        </span>
      ) : (
        <span className="muted">Akasha · 留一点时间，给阅读。</span>
      )}
    </footer>
  );
}
