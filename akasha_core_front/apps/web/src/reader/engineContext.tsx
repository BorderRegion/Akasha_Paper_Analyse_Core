/**
 * The PDF engine factory as a context.
 *
 * Production uses the locally bundled PDF.js; tests (jsdom has no canvas) inject
 * a deterministic engine. The reader component itself never imports PDF.js, so
 * the page lifecycle logic is testable and the heavy dependency stays lazy.
 */

import { createContext, createElement, useContext, type ReactElement, type ReactNode } from 'react';
import { createPdfJsEngine, type PageRenderer } from './pdfEngine';

export type PdfEngineFactory = (url: string) => Promise<PageRenderer>;

const defaultFactory: PdfEngineFactory = (url) => createPdfJsEngine({ url });

const PdfEngineContext = createContext<PdfEngineFactory>(defaultFactory);

export function PdfEngineProvider({
  factory,
  children,
}: {
  factory?: PdfEngineFactory;
  children: ReactNode;
}): ReactElement {
  return createElement(PdfEngineContext.Provider, { value: factory ?? defaultFactory }, children);
}

export function usePdfEngineFactory(): PdfEngineFactory {
  return useContext(PdfEngineContext);
}
