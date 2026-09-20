/**
 * A tiny context so any surface can open the import dialog without prop
 * drilling, while the dialog itself stays a single instance per app (docs/03
 * §S03: 关闭浮层不代表取消已接收任务 — one host owns the batch).
 */

import { createContext, useContext, useMemo, useState, type ReactElement, type ReactNode } from 'react';
import { ImportDialog } from './ImportDialog';

interface ImportDialogContextValue {
  open(): void;
  close(): void;
  isOpen: boolean;
}

const ImportDialogContext = createContext<ImportDialogContextValue | null>(null);

export function ImportDialogProvider({ children }: { children: ReactNode }): ReactElement {
  const [isOpen, setOpen] = useState(false);
  const value = useMemo<ImportDialogContextValue>(
    () => ({ isOpen, open: () => setOpen(true), close: () => setOpen(false) }),
    [isOpen],
  );
  return (
    <ImportDialogContext.Provider value={value}>
      {children}
      <ImportDialog open={isOpen} onClose={value.close} />
    </ImportDialogContext.Provider>
  );
}

export function useImportDialog(): ImportDialogContextValue {
  const value = useContext(ImportDialogContext);
  if (!value) throw new Error('useImportDialog must be used inside ImportDialogProvider');
  return value;
}
