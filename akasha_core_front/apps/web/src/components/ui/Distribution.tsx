import type { CSSProperties, ReactElement } from 'react';
import type { Tone } from '../../lib/status';
import styles from './distribution.module.css';

export interface DistributionItem {
  key: string;
  label: string;
  count: number;
  tone: Tone;
}

/** Counts, not confidence scores. Every segment has a text equivalent. */
export function Distribution({ label, items }: { label: string; items: DistributionItem[] }): ReactElement {
  const total = items.reduce((sum, item) => sum + item.count, 0);
  let offset = 0;
  return (
    <figure className={styles.figure} aria-label={label}>
      <div className={styles.ring}>
        <svg viewBox="0 0 120 120" aria-hidden="true">
          <circle cx="60" cy="60" r="48" fill="none" stroke="var(--border)" strokeWidth="9" />
          {items.filter(item => item.count > 0).map(item => {
            const part = total ? item.count / total * 100 : 0;
            const start = offset;
            offset += part;
            return <circle key={item.key} cx="60" cy="60" r="48" fill="none" stroke={`var(--${item.tone})`} strokeWidth="9" pathLength="100" strokeDasharray={`${part} ${100 - part}`} strokeDashoffset={-start} transform="rotate(-90 60 60)" />;
          })}
        </svg>
        <div className={styles.total}><strong>{total}</strong><span>合计</span></div>
      </div>
      <figcaption className={styles.caption}>
        <h3>{label}</h3>
        <ul>{items.map(item => <li key={item.key}><i aria-hidden="true" style={{ '--swatch': `var(--${item.tone})` } as CSSProperties} /><span>{item.label}</span><strong>{item.count}</strong></li>)}</ul>
        {!total ? <p className="muted">暂无记录</p> : null}
      </figcaption>
    </figure>
  );
}
