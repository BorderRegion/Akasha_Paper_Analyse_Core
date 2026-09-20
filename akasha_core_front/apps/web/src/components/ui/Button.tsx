import type { ButtonHTMLAttributes, ReactElement, ReactNode } from 'react';
import styles from './ui.module.css';

export type ButtonVariant = 'default' | 'primary' | 'quiet';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  children: ReactNode;
}

/** Text button. One primary action per page is a layout rule, not a prop. */
export function Button({ variant = 'default', children, ...rest }: ButtonProps): ReactElement {
  return (
    <button type="button" className={styles.button} data-variant={variant} {...rest}>
      {children}
    </button>
  );
}
