import { useRef, type ComponentProps, type ReactElement, type ReactNode } from 'react';
import * as Dialog from '@radix-ui/react-dialog';
import { Button } from './Button';
import styles from './modal.module.css';

type ModalProps = {
  open: boolean;
  onClose(): void;
  title: string;
  description?: string;
  children: ReactNode;
  returnFocusTo?: HTMLElement | null;
} & Omit<ComponentProps<typeof Dialog.Content>, 'children' | 'title'>;

/** All floating surfaces share keyboard, scroll-lock and focus-return behaviour. */
export function Modal({ open, onClose, title, description, children, returnFocusTo, className, ...props }: ModalProps): ReactElement {
  const previousFocus = useRef<HTMLElement | null>(null);
  const content = useRef<HTMLDivElement>(null);
  return <Dialog.Root open={open} onOpenChange={value => { if (!value) onClose(); }}>
    <Dialog.Portal>
      <Dialog.Overlay className={styles.overlay} />
      <Dialog.Content {...props} aria-modal="true" ref={content} className={`${styles.content} ${className ?? ''}`}
        {...(!description ? { 'aria-describedby': undefined } : {})}
        onOpenAutoFocus={event => {
          previousFocus.current = returnFocusTo ?? document.activeElement as HTMLElement | null;
          const target = content.current?.querySelector<HTMLElement>('[data-autofocus]');
          if (target) { event.preventDefault(); target.focus(); }
        }}
        onCloseAutoFocus={event => {
          const target = returnFocusTo ?? previousFocus.current;
          if (target?.isConnected) { event.preventDefault(); target.focus(); }
        }}>
        <header className={styles.header}>
          <Dialog.Title>{title}</Dialog.Title>
          <Button variant="quiet" onClick={onClose} aria-label={`关闭${title}`}>关闭 <span aria-hidden="true">×</span></Button>
        </header>
        {description ? <Dialog.Description className="muted">{description}</Dialog.Description> : null}
        {children}
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}
