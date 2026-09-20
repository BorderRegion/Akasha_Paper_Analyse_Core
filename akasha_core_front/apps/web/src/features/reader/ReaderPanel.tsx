/**
 * ReaderPanel (docs/03 §S05 + docs/07 §3).
 *
 * Layout: analysis column ≈34% / original ≈66%; the evidence Inspector REPLACES
 * the analysis column on demand (never three stacked drawers). Esc closes it and
 * "返回结论" goes back to the claim.
 *
 * Behaviour that must not drift:
 * - clicking a claim keeps the pinned version, requests that page's evidence and
 *   highlights only after the locator passed the version/hash check;
 * - a locator without geometry jumps to the page and says "只能定位到页面" — no
 *   invented highlight;
 * - the canvas is rendered lazily for the visible page ±1 and far pages are
 *   released; switching versions cancels in-flight renders so stale pixels can
 *   never appear under the new version's overlay.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import { useQuery } from '@tanstack/react-query';
import type { ClaimPreview, EvidenceLocator, PageEvidence } from '../../api/contract';
import { locate, type Rotation } from '../../reader/geometry';
import { PageWindow, type PageRenderer, type RenderedPage } from '../../reader/pdfEngine';
import { usePdfEngineFactory, type PdfEngineFactory } from '../../reader/engineContext';
import { EvidenceInspector } from './EvidenceInspector';
import type { ClaimEvidenceRow } from './EvidenceInspector';
import styles from './reader.module.css';

export interface ReaderPanelProps {
  paperVersionId: string;
  documentSha256: string;
  inspectorClaim: ClaimPreview | null;
  onCloseInspector(): void;
  onBackToClaims(): void;
  /** Test seam; production resolves the factory from PdfEngineProvider. */
  createEngine?: PdfEngineFactory;
  /** Test seam: initial page. */
  initialPage?: number;
  onPageChange?(page: number): void;
  onRememberPage?(page: number): void;
  remembering?: boolean;
}

const PAGE_WINDOW_RADIUS = 1;

export function ReaderPanel({
  paperVersionId,
  documentSha256,
  inspectorClaim,
  onCloseInspector,
  onBackToClaims,
  createEngine,
  initialPage = 1,
  onPageChange,
  onRememberPage,
  remembering = false,
}: ReaderPanelProps): ReactElement {
  const { client } = useSession();
  const contextFactory = usePdfEngineFactory();
  const [localPage, setLocalPage] = useState(initialPage);
  // The URL owns the page in the workspace. Mirroring it in a second state via
  // two effects can race a claim jump against a router update and jump back.
  const page = onPageChange ? initialPage : localPage;
  const [rotation, setRotation] = useState<Rotation>(0);
  const [renderer, setRenderer] = useState<PageRenderer | null>(null);
  const [engineError, setEngineError] = useState<string | null>(null);
  const [rendered, setRendered] = useState<RenderedPage[]>([]);
  const [focus, setFocus] = useState<{
    pageNumber: number;
    reason: string | null;
    locator: EvidenceLocator | null;
    box: { left: number; top: number; width: number; height: number } | null;
  } | null>(null);
  const windowRef = useRef(new PageWindow(PAGE_WINDOW_RADIUS));
  const pageRef = useRef(page);
  pageRef.current = page;
  const engineRef = useRef<PageRenderer | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const pageCallback = useRef(onPageChange);
  pageCallback.current = onPageChange;
  const [availableWidth, setAvailableWidth] = useState(0);
  const [fitWidth, setFitWidth] = useState(true);
  const setPage = useCallback((value: number | ((current: number) => number)) => {
    const next = typeof value === 'function' ? value(pageRef.current) : value;
    if (next === pageRef.current) return;
    if (pageCallback.current) pageCallback.current(next);
    else setLocalPage(next);
  }, []);
  useEffect(() => {
    if (!renderer) return;
    setPage(current => Math.max(1, Math.min(renderer.pageCount(), current)));
  }, [renderer, page, setPage]);
  useEffect(() => {
    const element = canvasRef.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(() => setAvailableWidth(Math.max(0, element.clientWidth - 28)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!inspectorClaim) return;
    containerRef.current?.scrollIntoView?.({ block: 'nearest' });
    containerRef.current?.focus({ preventScroll: true });
  }, [inspectorClaim?.claim_id]);

  // The claim→evidence link lives on the claim (core endpoint), NOT on the page
  // payload: without it the reader would highlight "some evidence on this page"
  // instead of the evidence that actually supports the claim being inspected.
  const claimEvidence = useQuery({
    queryKey: ['ui', 'claim-evidence', inspectorClaim?.claim_id ?? null] as const,
    enabled: Boolean(inspectorClaim?.claim_id),
    queryFn: async () => {
      const response = await client.request<{ claim_id: string; evidence: ClaimEvidenceRow[] }>(
        `/v1/ui/claims/${encodeURIComponent(inspectorClaim?.claim_id as string)}/evidence`,
      );
      return response.data.evidence;
    },
  });

  const pageEvidence = useQuery({
    queryKey: ['ui', 'page-evidence', paperVersionId, page] as const,
    queryFn: async () =>
      (await client.request<PageEvidence>(
        `/v1/ui/paper-versions/${encodeURIComponent(paperVersionId)}/pages/${page}/evidence`,
      )).data,
  });

  // A new version means a new document: cancel everything in flight FIRST, so a
  // late render cannot paint under the new version's overlay (UX-030).
  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    const pageWindow = windowRef.current;
    setEngineError(null);
    void (async () => {
      try {
        const factory = createEngine ?? contextFactory;
        const engine = await factory(
          `/v1/ui/paper-versions/${encodeURIComponent(paperVersionId)}/document`,
        );
        if (disposed) {
          await engine.destroy();
          return;
        }
        engineRef.current = engine;
        setRenderer(engine);
        pageWindow.attach(engine, pageRef.current);
      } catch (error) {
        if (!disposed) setEngineError(errorCodeOf(error) ?? 'STORAGE_003');
      }
    })();
    return () => {
      disposed = true;
      controller.abort();
      pageWindow.reset();
      // A version switch is a NEW document: tear the old one down so its
      // canvases are freed and a late render cannot paint under the new
      // version's overlay (docs/07 §3, UX-030).
      const previous = engineRef.current;
      engineRef.current = null;
      setRenderer(null);
      setRendered([]);
      void previous?.destroy();
    };
  }, [contextFactory, createEngine, paperVersionId, documentSha256]);

  // Lazy page window: the visible page plus neighbours are rendered; everything
  // else is released.
  useEffect(() => {
    if (!renderer) return;
    const pageWindow = windowRef.current;
    pageWindow.attach(renderer, page);
    setRendered((current) => current.filter((item) => Math.abs(item.pageNumber - page) <= PAGE_WINDOW_RADIUS));
    void pageWindow.sync(
      page,
      (page_) => {
        setRendered((current) => [
          ...current.filter((item) => item.pageNumber !== page_.pageNumber),
          page_,
        ]);
      },
      (pageNumber) => ({
        pageNumber,
        cssWidth: (pageEvidence.data?.page_display_width ?? 0) > 0 ? pageEvidence.data!.page_display_width : 612,
        cssHeight: (pageEvidence.data?.page_display_height ?? 0) > 0 ? pageEvidence.data!.page_display_height : 792,
        devicePixelRatio: typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1,
        rotation,
      }),
    ).catch((error) => setEngineError(errorCodeOf(error) ?? 'STORAGE_003'));
  }, [page, pageEvidence.data?.page_display_width, pageEvidence.data?.page_display_height, renderer, rotation]);

  const evidenceList = pageEvidence.data?.evidence ?? [];

  // A claim can point anywhere in the PDF, not only the currently visible page.
  useEffect(() => {
    setFocus(null);
    const source = claimEvidence.data?.find((row) =>
      (!row.paper_version_id || row.paper_version_id === paperVersionId) &&
      Number.isInteger(row.page_start) && (row.page_start ?? 0) > 0,
    );
    if (inspectorClaim && source?.page_start) setPage(source.page_start);
  }, [claimEvidence.data, inspectorClaim?.claim_id, paperVersionId]);

  // Auto-focus the first locator that belongs to the inspected claim AND passes
  // the version/hash check. A locator for another version is never selected.
  useEffect(() => {
    if (!inspectorClaim || !pageEvidence.data || !claimEvidence.data) return;
    const ids = new Set(claimEvidence.data.map((row) => row.evidence_id));
    const candidate = pageEvidence.data.evidence.find((locator) => ids.has(locator.evidence_id));
    if (!candidate) return;
    const outcome = locate(candidate, pageEvidence.data, {
      cssWidth: pageEvidence.data.page_display_width,
      cssHeight: pageEvidence.data.page_display_height,
      userRotation: rotation,
    });
    setFocus({
      pageNumber: candidate.page_number,
      reason: outcome.kind === 'BOX' ? null : outcome.reason,
      locator: candidate,
      box: outcome.kind === 'BOX' ? outcome.rect : null,
    });
  }, [claimEvidence.data, inspectorClaim, pageEvidence.data, rotation]);
  const boxFor = useMemo(() => {
    if (!focus?.locator || !pageEvidence.data) return null;
    const outcome = locate(focus.locator, pageEvidence.data, {
      cssWidth: pageEvidence.data.page_display_width,
      cssHeight: pageEvidence.data.page_display_height,
      userRotation: rotation,
    });
    return outcome.kind === 'BOX' ? outcome.rect : null;
  }, [focus?.locator, pageEvidence.data, rotation]);
  const nativeWidth = pageEvidence.data?.page_display_width || 612;
  const nativeHeight = pageEvidence.data?.page_display_height || 792;
  const rotated = rotation === 90 || rotation === 270;
  const displayWidth = rotated ? nativeHeight : nativeWidth;
  const displayHeight = rotated ? nativeWidth : nativeHeight;
  const scale = fitWidth && availableWidth > 0 ? Math.min(1, availableWidth / displayWidth) : 1;

  return (
    <aside
      className={styles.reader}
      aria-label="原文与证据阅读器"
      data-testid="reader-panel"
      ref={containerRef}
      tabIndex={-1}
      onKeyDown={(event) => {
        if (event.key === 'Escape') onCloseInspector();
      }}
    >
      <div className={styles.toolbar}>
        <Button variant="quiet" disabled={page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>
          上一页
        </Button>
        <label>
          页码
          <input
            type="number"
            min={1}
            value={page}
            aria-label="页码"
            max={renderer?.pageCount()}
            onChange={(event) => setPage(Math.min(renderer?.pageCount() ?? Infinity,
              Math.max(1, Math.trunc(Number(event.target.value)) || 1)))}
          />
        </label>
        <Button
          variant="quiet"
          disabled={!renderer || page >= renderer.pageCount()}
          onClick={() => setPage((current) => Math.min(renderer?.pageCount() ?? current + 1, current + 1))}
        >
          下一页
        </Button>
        <Button
          variant="quiet"
          onClick={() => setRotation(((rotation + 90) % 360) as Rotation)}
          aria-label="旋转原文"
        >
          旋转 {rotation}°
        </Button>
        <span className="muted">
          {page} / {renderer?.pageCount() ?? '…'} 页
        </span>
        <Button variant="quiet" aria-pressed={fitWidth} onClick={() => setFitWidth(value => !value)}>{fitWidth ? '适合宽度' : '原始大小'}</Button>
        {onRememberPage ? <Button variant="quiet" disabled={remembering || !renderer} onClick={() => onRememberPage(page)}>记住这一页</Button> : null}
      </div>

      {inspectorClaim ? (
        <AsyncBoundary state={claimEvidence.isPending ? 'loading' : claimEvidence.isError ? 'error' : 'ready'} errorCode={errorCodeOf(claimEvidence.error)} onRetry={() => void claimEvidence.refetch()}>
        <EvidenceInspector
          claim={inspectorClaim}
          locator={focus?.locator ?? null}
          pageEvidence={pageEvidence.data ?? null}
          evidenceRow={
            (claimEvidence.data ?? []).find(
              (row) => row.evidence_id === focus?.locator?.evidence_id,
            ) ?? null
          }
          onBackToClaims={onBackToClaims}
        />
        </AsyncBoundary>
      ) : null}

      {focus?.reason ? (
        <p role="status" data-testid="locate-degraded">
          {focus.reason}（仅定位到第 {focus.pageNumber} 页，不绘制高亮）
        </p>
      ) : null}

      <div ref={canvasRef} className={styles.canvasArea} data-testid="canvas-area" tabIndex={0} role="region" aria-label="PDF 原文，可滚动">
        {engineError ? (
          <AsyncBoundary state="error" errorCode={engineError}>
            <p className="muted">原文不可用时可阅读已提取文本，阅读不会因此中断。</p>
          </AsyncBoundary>
        ) : null}
        {rendered.filter((item) => item.pageNumber === page).map((item) => (
          <div key={item.pageNumber} className={styles.pageBox} data-page={item.pageNumber}>
            <span className="muted">第 {item.pageNumber} 页</span>
            <div style={{ position: 'relative', width: displayWidth * scale, height: displayHeight * scale, overflow: 'hidden' }}>
            <div style={{ position: 'relative', width: displayWidth, height: displayHeight, transform: `scale(${scale})`, transformOrigin: 'top left' }}>
            <PageSurface surface={item.surface} />
            {boxFor && item.pageNumber === focus?.pageNumber ? (
              <div
                className={styles.highlight}
                data-testid="evidence-box"
                style={{
                  left: `${boxFor.left}px`,
                  top: `${boxFor.top}px`,
                  width: `${boxFor.width}px`,
                  height: `${boxFor.height}px`,
                }}
              />
            ) : null}
            </div>
            </div>
          </div>
        ))}
        {!rendered.some((item) => item.pageNumber === page) && !engineError ? <AsyncBoundary state="loading" /> : null}
      </div>

      <details className={styles.evidenceList}>
        <summary>本页证据（{pageEvidence.data ? evidenceList.length : '未载入'}）</summary>
        {pageEvidence.isError ? <AsyncBoundary state="error" errorCode={errorCodeOf(pageEvidence.error)}
          onRetry={() => void pageEvidence.refetch()} /> : null}
        <ul>
          {evidenceList.map((locator) => (
            <li key={locator.evidence_id}>
              <span className="muted">
                {locator.precision === 'REGION'
                  ? '可定位区域'
                  : locator.precision === 'PAGE'
                    ? '只能定位到页面'
                    : '仅有文本'}
              </span>
              {locator.reason ? <span className="muted">（{locator.reason}）</span> : null}
              <Button
                variant="quiet"
                onClick={() => {
                  if (!pageEvidence.data) return;
                  const outcome = locate(locator, pageEvidence.data, {
                    cssWidth: pageEvidence.data.page_display_width,
                    cssHeight: pageEvidence.data.page_display_height,
                    userRotation: rotation,
                  });
                  setFocus({
                    pageNumber: locator.page_number,
                    reason: outcome.kind === 'BOX' ? null : outcome.reason,
                    locator,
                    box: outcome.kind === 'BOX' ? outcome.rect : null,
                  });
                }}
              >
                定位
              </Button>
            </li>
          ))}
        </ul>
      </details>

      <div className={styles.footer}>
        <Button variant="quiet" onClick={onBackToClaims}>
          返回结论
        </Button>
        <span className="muted">按 Esc 收起证据详情。</span>
      </div>
    </aside>
  );
}

/** PDF.js produces an imperative canvas; React must actually mount that node. */
function PageSurface({ surface }: { surface: unknown }): ReactElement {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const element = host.current;
    if (!element || !(surface instanceof HTMLCanvasElement)) return;
    surface.style.display = 'block';
    element.replaceChildren(surface);
    return () => { if (surface.parentNode === element) surface.remove(); };
  }, [surface]);
  return <div ref={host} data-testid="pdf-page-surface" />;
}
