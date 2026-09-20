/**
 * PDF engine boundary + lazy page window (spec docs/07 §3).
 *
 * The reader talks to a small interface, not to PDF.js directly:
 * - production uses the locally bundled worker (`createPdfJsEngine`), never a CDN;
 * - tests inject a deterministic engine, so page-lifecycle behaviour (lazy load,
 *   neighbour prefetch, canvas release, render-task cancellation) is testable
 *   without a canvas implementation;
 * - `release()` actually frees the canvas: a long PDF must not keep every
 *   rendered bitmap alive.
 */

export interface RenderPageRequest {
  pageNumber: number;
  /** CSS size of the page box; the canvas backing store uses DPR for sharpness. */
  cssWidth: number;
  cssHeight: number;
  devicePixelRatio: number;
  rotation: 0 | 90 | 180 | 270;
}

export interface RenderedPage {
  pageNumber: number;
  /** Opaque handle the engine owns (canvas, bitmap, …). */
  surface: unknown;
  width: number;
  height: number;
}

export interface PageRenderer {
  /** Total page count of the opened document. */
  pageCount(): number;
  render(request: RenderPageRequest, signal?: AbortSignal): Promise<RenderedPage>;
  /**
   * Cancel any in-flight render for this page. Called when the version changes
   * or the page scrolls away — a stale render completing later must never paint
   * over a newer paper/version (docs/07 §3, UX-030).
   */
  cancel(pageNumber: number): void;
  /** Free the canvas/bitmap for a page that is no longer near the viewport. */
  release(pageNumber: number): void;
  /** Pages the engine currently holds surfaces for (test/diagnostic seam). */
  livePageNumbers(): number[];
  destroy(): Promise<void>;
}

export interface PageWindowOptions {
  /** How many neighbours around the visible page stay rendered. */
  neighbourRadius?: number;
  /** Extra pages beyond the neighbour window are released immediately. */
  currentPage: number;
  pageCount: number;
}

/** Pages that should be rendered: the visible page plus its neighbours. */
export function pagesToRender({ currentPage, pageCount, neighbourRadius = 1 }: PageWindowOptions): number[] {
  const pages: number[] = [];
  for (
    let page = Math.max(1, currentPage - neighbourRadius);
    page <= Math.min(pageCount, currentPage + neighbourRadius);
    page += 1
  ) {
    pages.push(page);
  }
  return pages;
}

/** Pages that must be released: everything outside the window. */
export function pagesToRelease(
  live: number[],
  options: PageWindowOptions,
): number[] {
  const keep = new Set(pagesToRender(options));
  return live.filter((page) => !keep.has(page));
}

/**
 * Loads and releases pages as the reader scrolls.
 *
 * The owner (a React component) calls `sync()` whenever the visible page or the
 * document changes. Everything else — cancellation, release, avoiding a stale
 * paint — happens here so it can be tested directly.
 */
export class PageWindow {
  private renderer: PageRenderer | null = null;
  private currentPage = 1;
  private readonly neighbourRadius: number;
  private readonly tokens = new Map<number, symbol>();
  private readonly requests = new Map<number, string>();
  private inflight = new Map<number, AbortController>();

  constructor(neighbourRadius = 1) {
    this.neighbourRadius = neighbourRadius;
  }

  attach(renderer: PageRenderer, currentPage = 1): void {
    if (this.renderer !== renderer) this.reset();
    this.renderer = renderer;
    this.currentPage = currentPage;
  }

  /** A new document: drop every token so no pending render may paint. */
  reset(): void {
    for (const controller of this.inflight.values()) controller.abort();
    this.inflight.clear();
    this.tokens.clear();
    this.requests.clear();
    this.renderer = null;
  }

  async sync(
    currentPage: number,
    onRendered: (page: RenderedPage) => void,
    requestFor: (pageNumber: number) => RenderPageRequest,
  ): Promise<void> {
    const renderer = this.renderer;
    if (!renderer) return;
    this.currentPage = currentPage;
    const pageCount = renderer.pageCount();
    const wanted = pagesToRender({
      currentPage,
      pageCount,
      neighbourRadius: this.neighbourRadius,
    });

    for (const page of pagesToRelease([...new Set([...renderer.livePageNumbers(), ...this.tokens.keys()])], {
      currentPage,
      pageCount,
      neighbourRadius: this.neighbourRadius,
    })) {
      renderer.cancel(page);
      this.inflight.get(page)?.abort();
      this.inflight.delete(page);
      renderer.release(page);
      this.tokens.delete(page);
      this.requests.delete(page);
    }

    await Promise.all(
      wanted.map(async (page) => {
        const request = requestFor(page);
        const signature = JSON.stringify(request);
        if (this.tokens.has(page) && this.requests.get(page) === signature) return;
        this.inflight.get(page)?.abort();
        renderer.cancel(page);
        const token = Symbol(`page-${page}`);
        this.tokens.set(page, token);
        this.requests.set(page, signature);
        const controller = new AbortController();
        this.inflight.set(page, controller);
        try {
          const rendered = await renderer.render(request, controller.signal);
          if (!this.isCurrent(rendered.pageNumber, token)) {
            // A newer document/version took over: do not paint the stale page.
            if (this.renderer !== renderer || !this.tokens.has(page)) renderer.release(rendered.pageNumber);
            return;
          }
          onRendered(rendered);
        } catch (error) {
          if (!this.isCurrent(page, token)) return;
          this.tokens.delete(page);
          this.requests.delete(page);
          if ((error as Error)?.name === 'RenderingCancelledException') return;
          throw error;
        } finally {
          if (this.inflight.get(page) === controller) this.inflight.delete(page);
        }
      }),
    );
  }

  private isCurrent(pageNumber: number, token: symbol): boolean {
    return this.tokens.get(pageNumber) === token;
  }

  /** True when a render for this page may still paint. */
  isRenderable(pageNumber: number): boolean {
    return this.tokens.has(pageNumber);
  }

  get visiblePage(): number {
    return this.currentPage;
  }
}

interface PdfJsPage {
  rotate?: number;
  getViewport(params: { scale: number; rotation?: number }): {
    width: number;
    height: number;
  };
  render(params: {
    canvasContext: CanvasRenderingContext2D;
    viewport: unknown;
  }): { promise: Promise<void>; cancel(): void };
  cleanup?(): void;
}

interface PdfJsDocument {
  numPages: number;
  getPage(pageNumber: number): Promise<PdfJsPage>;
  destroy(): Promise<void>;
}

export interface PdfJsEngineOptions {
  url: string;
  /** Device pixel ratio used for canvas sharpness only. */
  devicePixelRatio?: number;
}

/**
 * The real engine. PDF.js is imported dynamically so the initial bundle stays
 * small, and the worker is bundled locally (docs/07 §3: never from a CDN).
 */
export async function createPdfJsEngine(options: PdfJsEngineOptions): Promise<PageRenderer> {
  const pdfjs = await import('pdfjs-dist');
  const worker = await import('pdfjs-dist/build/pdf.worker.min.mjs?url');
  pdfjs.GlobalWorkerOptions.workerSrc = (worker as { default: string }).default;
  const document_ = (await pdfjs.getDocument({ url: options.url }).promise) as unknown as PdfJsDocument;
  const canvases = new Map<number, HTMLCanvasElement>();
  const tasks = new Map<number, { cancel(): void }>();
  const generations = new Map<number, symbol>();

  return {
    pageCount: () => document_.numPages,
    async render(request, signal) {
      const generation = Symbol();
      tasks.get(request.pageNumber)?.cancel();
      generations.set(request.pageNumber, generation);
      const checkCurrent = () => {
        if (signal?.aborted || generations.get(request.pageNumber) !== generation) {
          const error = new Error('Page render superseded');
          error.name = 'RenderingCancelledException';
          throw error;
        }
      };
      const page = await document_.getPage(request.pageNumber);
      checkCurrent();
      const dpr = options.devicePixelRatio ?? request.devicePixelRatio;
      const scale = 1;
      const viewport = page.getViewport({ scale: scale * dpr, rotation: ((page.rotate ?? 0) + request.rotation) % 360 });
      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(viewport.width);
      canvas.height = Math.floor(viewport.height);
      const swaps = request.rotation === 90 || request.rotation === 270;
      canvas.style.width = `${swaps ? request.cssHeight : request.cssWidth}px`;
      canvas.style.height = `${swaps ? request.cssWidth : request.cssHeight}px`;
      const context = canvas.getContext('2d');
      if (!context) throw new Error('canvas 2d context unavailable');
      const task = page.render({ canvasContext: context, viewport });
      tasks.set(request.pageNumber, task);
      const abort = () => task.cancel();
      signal?.addEventListener('abort', abort, { once: true });
      try {
        await task.promise;
        checkCurrent();
      } finally {
        if (tasks.get(request.pageNumber) === task) tasks.delete(request.pageNumber);
        signal?.removeEventListener('abort', abort);
      }
      canvases.set(request.pageNumber, canvas);
      return {
        pageNumber: request.pageNumber,
        surface: canvas,
        width: canvas.width,
        height: canvas.height,
      };
    },
    cancel(pageNumber) {
      generations.delete(pageNumber);
      tasks.get(pageNumber)?.cancel();
      tasks.delete(pageNumber);
    },
    release(pageNumber) {
      const canvas = canvases.get(pageNumber);
      if (canvas) {
        // Shrinking to 1x1 lets the browser reclaim the backing store.
        canvas.width = 1;
        canvas.height = 1;
      }
      canvases.delete(pageNumber);
    },
    livePageNumbers: () => [...canvases.keys()],
    async destroy() {
      generations.clear();
      for (const task of tasks.values()) task.cancel();
      tasks.clear();
      canvases.clear();
      await document_.destroy();
    },
  };
}
