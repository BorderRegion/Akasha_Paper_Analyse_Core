/**
 * A deterministic PDF engine for tests (jsdom has no canvas).
 *
 * It records the page lifecycle — render, cancel, release — so the reader's lazy
 * loading and version-race behaviour can be asserted directly instead of being
 * inferred from pixels.
 */

import type { PageRenderer, RenderPageRequest, RenderedPage } from '../../src/reader/pdfEngine';

export interface FakeEngine extends PageRenderer {
  renderedPages: number[];
  cancelled: number[];
  released: number[];
  /** Resolve the promise a pending render is waiting on (per page). */
  pending: Map<number, () => void>;
  /** When true, renders wait until `release(page)` is called. */
  hold: boolean;
  destroyed: boolean;
}

export function createFakeEngine(pageCount = 5): FakeEngine {
  const canvases = new Map<number, HTMLCanvasElement>();
  const pending = new Map<number, () => void>();
  const engine: FakeEngine = {
    renderedPages: [],
    cancelled: [],
    released: [],
    pending,
    hold: false,
    destroyed: false,
    pageCount: () => pageCount,
    async render(request: RenderPageRequest): Promise<RenderedPage> {
      if (engine.hold) {
        await new Promise<void>((resolve) => pending.set(request.pageNumber, resolve));
      }
      const canvas = document.createElement('canvas');
      canvas.width = request.cssWidth;
      canvas.height = request.cssHeight;
      canvas.style.width = `${request.cssWidth}px`;
      canvas.style.height = `${request.cssHeight}px`;
      canvases.set(request.pageNumber, canvas);
      engine.renderedPages.push(request.pageNumber);
      return {
        pageNumber: request.pageNumber,
        surface: canvas,
        width: request.cssWidth,
        height: request.cssHeight,
      };
    },
    cancel(pageNumber: number) {
      engine.cancelled.push(pageNumber);
      pending.get(pageNumber)?.();
      pending.delete(pageNumber);
    },
    release(pageNumber: number) {
      engine.released.push(pageNumber);
      canvases.delete(pageNumber);
    },
    livePageNumbers: () => [...canvases.keys()],
    async destroy() {
      engine.destroyed = true;
      canvases.clear();
    },
  };
  return engine;
}
