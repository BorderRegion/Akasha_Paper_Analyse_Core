import { useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import { Modal } from '../../components/ui/Modal';
import styles from './shell.module.css';

export interface CommandPaletteProps {
  open: boolean;
  onClose(): void;
  returnFocusTo?: HTMLElement | null;
  children?: ReactElement;
  onSearch?(query: string): void;
  onNavigate?(path: string): void;
}

export function CommandPalette({ open, onClose, returnFocusTo, children, onSearch, onNavigate }: CommandPaletteProps): ReactElement {
  const [query, setQuery] = useState('');
  return <Modal open={open} onClose={onClose} title="命令与搜索" returnFocusTo={returnFocusTo}
    className={styles.palette} data-testid="command-palette">
    <form className={styles.paletteSearch} onSubmit={event => { event.preventDefault(); if (query.trim()) onSearch?.(query.trim()); }}>
      <label htmlFor="command-input" className="srOnly">命令或搜索</label>
      <input id="command-input" data-autofocus placeholder="搜索论文，按 Enter 确认" value={query}
        onChange={event => setQuery(event.target.value)} />
      <Button type="submit" disabled={!query.trim()} variant="primary">搜索</Button>
    </form>
    {onNavigate ? <nav aria-label="快捷前往" className={styles.paletteLinks}>
      {[['/app/library', '文献库'], ['/app/review', '待核查'], ['/app/operations', '运行状态'], ['/app/settings', '设置']].map(([path, label]) =>
        <Button key={path} variant="quiet" onClick={() => onNavigate(path!)}>{label} <span aria-hidden="true">→</span></Button>)}
    </nav> : null}
    {children}
  </Modal>;
}
