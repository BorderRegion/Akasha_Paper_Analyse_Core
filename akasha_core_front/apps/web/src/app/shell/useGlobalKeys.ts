import { useEffect } from 'react';
import { isComposing, isEditableTarget } from '../../lib/a11y';

export interface GlobalKeyHandlers {
  onCommandPalette: () => void;
  onFocusSearch: () => void;
  onToggleFocusMode: () => void;
  /** Single-character shortcuts can be disabled in settings (docs/02). */
  singleKeyShortcuts: boolean;
}

/**
 * Global keyboard contract (docs/02 §键盘与发现性):
 * - Ctrl/Cmd+K opens the command palette anywhere;
 * - `/` focuses global search only when not typing;
 * - `F` toggles focus mode in the reader only (page opt-in);
 * - Alt+Left/Right are never intercepted (browser history);
 * - IME composition suppresses every single-character shortcut.
 */
export function useGlobalKeys(handlers: GlobalKeyHandlers): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // Never hijack browser history navigation.
      if (event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) return;
      if (event.defaultPrevented || document.querySelector('[role="dialog"][aria-modal="true"]')) return;

      const isCommandKey = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k';
      if (isCommandKey) {
        event.preventDefault();
        handlers.onCommandPalette();
        return;
      }

      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isComposing(event)) return; // Chinese IME: composing must not fire actions
      if (isEditableTarget(event.target)) return;
      if (!handlers.singleKeyShortcuts) return;
      if (event.repeat) return;

      if (event.key === '/') {
        event.preventDefault();
        handlers.onFocusSearch();
      } else if (event.key.toLowerCase() === 'f') {
        handlers.onToggleFocusMode();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [handlers]);
}
