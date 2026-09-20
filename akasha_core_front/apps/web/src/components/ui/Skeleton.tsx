import type { ReactElement } from 'react';
import styles from './ui.module.css';

export interface SkeletonProps {
  height?: number;
  width?: string;
  label?: string;
}

/** Static-shape placeholder; its pulse is disabled under reduced motion. */
export function Skeleton({ height = 16, width = '100%', label = '正在载入' }: SkeletonProps): ReactElement {
  return (
    <div
      className={`${styles.skeleton} skeleton`}
      style={{ height, width }}
      role="status"
      aria-live="polite"
      aria-label={label}
    />
  );
}
