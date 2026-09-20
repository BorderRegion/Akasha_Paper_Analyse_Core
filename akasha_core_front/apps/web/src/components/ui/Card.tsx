import type { ReactElement, ReactNode } from 'react';
import styles from './ui.module.css';

export interface CardProps {
  title?: string;
  actions?: ReactNode;
  children: ReactNode;
}

export function Card({ title, actions, children }: CardProps): ReactElement {
  return (
    <section className={styles.card} aria-label={title}>
      {title || actions ? (
        <header className={styles.row} style={{ justifyContent: 'space-between' }}>
          {title ? <h3>{title}</h3> : <span />}
          {actions}
        </header>
      ) : null}
      {children}
    </section>
  );
}
