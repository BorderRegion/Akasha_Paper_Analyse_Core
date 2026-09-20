import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { Outlet, useNavigate } from 'react-router-dom';
import { CommandPalette } from './CommandPalette';
import { Sidebar } from './Sidebar';
import { StatusBar } from './StatusBar';
import { useGlobalKeys } from './useGlobalKeys';
import styles from './shell.module.css';
import { IconButton } from '../../components/ui/IconButton';

export interface ShellProps {
  /** Toggle for single-character shortcuts (settings). */
  singleKeyShortcuts?: boolean;
  defaultFocus?: boolean;
  processing?: number;
  needsAttention?: number;
  /** Search submit is owned by the page; the shell only focuses the field. */
  onSearchSubmit?: (query: string) => void;
}

/**
 * AppShell: routing frame, navigation and the status line. It owns no paper
 * analysis (docs/05 component table) and never fetches data itself.
 */
export function Shell({
  singleKeyShortcuts = true,
  defaultFocus = false,
  processing = 0,
  needsAttention = 0,
  onSearchSubmit,
}: ShellProps): ReactElement {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [focusMode, setFocusMode] = useState(defaultFocus);
  useEffect(() => setFocusMode(defaultFocus), [defaultFocus]);
  const searchRef = useRef<HTMLInputElement>(null);
  const paletteTriggerRef = useRef<HTMLButtonElement>(null);
  const navigate = useNavigate();

  const handlers = useMemo(
    () => ({
      onCommandPalette: () => setPaletteOpen(true),
      onFocusSearch: () => searchRef.current?.focus(),
      onToggleFocusMode: () => setFocusMode((value) => !value),
      singleKeyShortcuts,
    }),
    [singleKeyShortcuts],
  );
  useGlobalKeys(handlers);

  return (
    <div className={styles.shell} data-focus={String(focusMode)}>
      <a className={styles.skipLink} href="#main-content">
        跳到主要内容
      </a>
      <Sidebar />
      <header className={styles.toolbar}>
        <form
          className={styles.searchForm}
          role="search"
          onSubmit={(event) => {
            event.preventDefault();
            const value = searchRef.current?.value ?? '';
            onSearchSubmit?.(value);
            navigate(`/app/library?q=${encodeURIComponent(value)}`);
          }}
        >
          <label htmlFor="global-search" className="srOnly">
            全局搜索
          </label>
          <input
            id="global-search"
            ref={searchRef}
            className="input"
            type="search"
            placeholder="找一篇论文，或一个想法…"
          />
        </form>
        <div className={styles.toolbarSpacer} />
        <IconButton
          ref={paletteTriggerRef}
          label="命令与搜索（Ctrl+K）"
          aria-keyshortcuts="Control+K Meta+K"
          onClick={() => setPaletteOpen(true)}
          icon={<span aria-hidden="true">⌘</span>}
        />
        <IconButton
          label={focusMode ? '退出专注模式' : '进入专注模式（F）'}
          aria-pressed={focusMode}
          onClick={() => setFocusMode((value) => !value)}
          icon={<span aria-hidden="true">◎</span>}
        />
      </header>
      <main id="main-content" className={styles.main} tabIndex={-1}>
        <Outlet />
      </main>
      <StatusBar processing={processing} needsAttention={needsAttention} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        returnFocusTo={paletteTriggerRef.current}
        onSearch={query => { navigate(`/app/library?q=${encodeURIComponent(query)}`); setPaletteOpen(false); }}
        onNavigate={path => { navigate(path); setPaletteOpen(false); }}
      />
    </div>
  );
}
