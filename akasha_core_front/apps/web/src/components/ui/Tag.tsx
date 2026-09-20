import type { ReactElement } from 'react';
import styles from './ui.module.css';

export interface TagProps {
  label: string;
  namespace?: string;
  candidate?: boolean;
}

/** A tag is a label, not a score. Candidate tags are marked, not promoted. */
export function Tag({ label, namespace, candidate }: TagProps): ReactElement {
  return (
    <span className={styles.tag} data-candidate={candidate ? 'true' : undefined} title={namespace}>
      {candidate ? '候选 · ' : ''}
      {label}
    </span>
  );
}
