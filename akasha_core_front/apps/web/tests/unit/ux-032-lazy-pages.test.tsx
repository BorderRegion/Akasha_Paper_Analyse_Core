/**
 * UX-032 — pages load lazily and far canvases are released.
 *
 * Requirement (docs/07 §3): 只加载可见页及相邻1页，远页释放canvas。取消切换前的
 * RenderTask；避免旧请求完成后覆盖新paper/version画布.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import {
  PageWindow,
  pagesToRelease,
  pagesToRender,
} from '../../src/reader/pdfEngine';
import { createFakeEngine } from '../support/fake-engine';
import { createFakeServer } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';

describe('UX-032 lazy page loading and canvas release', () => {
  it('reattaching the same document does not cancel an unchanged in-flight page', async () => {
    const engine = createFakeEngine(4);
    engine.hold = true;
    const window_ = new PageWindow(0);
    const request = (pageNumber: number) => ({ pageNumber, cssWidth: 612, cssHeight: 792,
      devicePixelRatio: 1, rotation: 0 as const });
    const paint = vi.fn();
    window_.attach(engine, 3);
    const pending = window_.sync(3, paint, request);
    window_.attach(engine, 3);
    await window_.sync(3, paint, request);
    engine.pending.get(3)?.();
    await pending;
    expect(paint).toHaveBeenCalledTimes(1);
    expect(engine.renderedPages).toEqual([3]);
  });

  it('an older cancelled render cannot erase the replacement token', async () => {
    const engine = createFakeEngine(4);
    let rejectOld!: (error: Error) => void;
    engine.render = vi.fn().mockImplementationOnce(() => new Promise((_, reject) => { rejectOld = reject; }))
      .mockResolvedValue({ pageNumber: 3, surface: document.createElement('canvas'), width: 792, height: 612 });
    const window_ = new PageWindow(0);
    const request = { pageNumber: 3, cssWidth: 612, cssHeight: 792, devicePixelRatio: 1, rotation: 0 as const };
    window_.attach(engine, 3);
    const paint = vi.fn();
    const old = window_.sync(3, paint, () => request);
    await window_.sync(3, paint, () => ({ ...request, rotation: 90 }));
    const error = new Error('cancelled');
    error.name = 'RenderingCancelledException';
    rejectOld(error);
    await old;
    expect(window_.isRenderable(3)).toBe(true);
    expect(paint).toHaveBeenCalledTimes(1);
  });

  it('renders only the visible page and its neighbours', () => {
    expect(pagesToRender({ currentPage: 1, pageCount: 5 })).toEqual([1, 2]);
    expect(pagesToRender({ currentPage: 3, pageCount: 5 })).toEqual([2, 3, 4]);
    expect(pagesToRender({ currentPage: 5, pageCount: 5 })).toEqual([4, 5]);
    expect(pagesToRender({ currentPage: 3, pageCount: 10, neighbourRadius: 0 })).toEqual([3]);
  });

  it('releases every page outside the window and keeps the window', () => {
    const live = [1, 2, 3, 4, 5, 6];
    expect(pagesToRelease(live, { currentPage: 4, pageCount: 10 })).toEqual([1, 2, 6]);
  });

  it('renders neighbours when the page changes and frees the pages left behind', async () => {
    const engine = createFakeEngine(6);
    const window_ = new PageWindow(1);
    window_.attach(engine, 1);
    const painted: number[] = [];
    const request = (pageNumber: number) => ({
      pageNumber,
      cssWidth: 600,
      cssHeight: 800,
      devicePixelRatio: 1,
      rotation: 0 as const,
    });

    await window_.sync(1, (page) => painted.push(page.pageNumber), request);
    expect(engine.renderedPages).toEqual([1, 2]);

    await window_.sync(4, (page) => painted.push(page.pageNumber), request);
    expect(engine.renderedPages).toEqual([1, 2, 3, 4, 5]);
    // 1 and 2 are far away now and must be released, not kept alive.
    expect(engine.released.sort()).toEqual([1, 2]);
    expect(engine.livePageNumbers().sort()).toEqual([3, 4, 5]);
  });

  it('cancels in-flight renders when the document changes', async () => {
    const engine = createFakeEngine(4);
    engine.hold = true;
    const window_ = new PageWindow(1);
    window_.attach(engine, 1);
    const request = (pageNumber: number) => ({
      pageNumber,
      cssWidth: 600,
      cssHeight: 800,
      devicePixelRatio: 1,
      rotation: 0 as const,
    });

    const syncPromise = window_.sync(1, () => undefined, request);
    window_.reset();
    // Everything in flight was aborted; the pending render resolves but the
    // result is not painted.
    for (const release of engine.pending.values()) release();
    await syncPromise;
    expect(engine.livePageNumbers()).toHaveLength(0);
  });

  it('releases the canvas of a far page in the real reader as the page changes', async () => {
    const engine = createFakeEngine(6);
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Long paper' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
    });
    renderApp(server, {
      path: '/app/papers/pap_1',
      engineFactory: async () => engine,
    });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '原文' }));
    await screen.findByTestId('reader-panel');

    // Walk forward: the earlier pages must be released as they leave the window.
    for (let step = 0; step < 4; step += 1) {
      await user.click(screen.getByRole('button', { name: '下一页' }));
    }
    await waitFor(() => expect(engine.renderedPages).toContain(5), { timeout: 3000 });
    await waitFor(() => expect(engine.released.length).toBeGreaterThan(0));
    expect(engine.livePageNumbers()).not.toContain(1);
  });

  it('does not keep a canvas for every visited page', async () => {
    const engine = createFakeEngine(20);
    const window_ = new PageWindow(1);
    window_.attach(engine, 1);
    const request = (pageNumber: number) => ({
      pageNumber,
      cssWidth: 600,
      cssHeight: 800,
      devicePixelRatio: 1,
      rotation: 0 as const,
    });
    for (const page of [1, 2, 3, 4, 5, 6, 7, 8]) {
      await window_.sync(page, () => undefined, request);
    }
    expect(engine.renderedPages.length).toBeGreaterThan(5);
    // Never more than the window: a 20-page document does not hold 20 canvases.
    expect(engine.livePageNumbers().length).toBeLessThanOrEqual(3);
  });

  it('renders at most the window on a long document', async () => {
    const engine = createFakeEngine(120);
    const window_ = new PageWindow(1);
    window_.attach(engine, 60);
    await window_.sync(
      60,
      () => undefined,
      (pageNumber) => ({
        pageNumber,
        cssWidth: 600,
        cssHeight: 800,
        devicePixelRatio: 2,
        rotation: 0 as const,
      }),
    );
    expect(engine.renderedPages).toEqual([59, 60, 61]);
  });
});
