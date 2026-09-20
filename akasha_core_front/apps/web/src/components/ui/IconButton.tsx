import type { ButtonHTMLAttributes, ReactElement, Ref } from 'react';
import styles from './ui.module.css';

export interface IconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'aria-label'> {
  /** Required: an icon-only control must carry its name (UX-008). */
  label: string;
  icon: ReactElement;
  ref?: Ref<HTMLButtonElement>;
}

export function IconButton({ label, icon, ref, ...rest }: IconButtonProps): ReactElement {
  return (
    <button
      type="button"
      ref={ref}
      className={styles.iconButton}
      aria-label={label}
      title={label}
      {...rest}
    >
      {icon}
    </button>
  );
}
