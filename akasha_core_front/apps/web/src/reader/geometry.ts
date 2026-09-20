/**
 * Reader geometry (spec docs/07 §2).
 *
 * The ONE place a stored `rect_norm` becomes a CSS box.
 *
 * Facts encoded here, because getting them wrong is the classic "looks right,
 * is wrong" bug:
 * - `rect_norm` is ALREADY normalized to the displayed page with the PDF's
 *   built-in rotation and crop applied, origin top-left, every value in [0,1];
 * - `page_display_width/height` describe the crop-adjusted displayed aspect, so
 *   the CSS box comes from them — the raw bbox is never multiplied by the
 *   browser width;
 * - `devicePixelRatio` affects canvas SHARPNESS only. It must never be applied
 *   to the overlay, or highlights drift by the DPR factor;
 * - a user rotation (0/90/180/270) transforms all four corners and takes the
 *   bounds — rotating the canvas while leaving the overlay alone is a bug;
 * - browser zoom and the page's CSS size change the pixels on screen but not the
 *   normalized rectangle.
 */

import type { EvidenceLocator, PageEvidence } from '../api/contract';

export type Rotation = 0 | 90 | 180 | 270;

export interface RectNorm {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface PageBox {
  pageNumber: number;
  /** Crop-adjusted displayed size at scale 1 (from the server). */
  displayWidth: number;
  displayHeight: number;
  referenceRotation: Rotation;
}

export interface CssRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export type LocatorOutcome =
  | { kind: 'BOX'; rect: CssRect; locator: EvidenceLocator }
  | {
      kind: 'PAGE_ONLY';
      pageNumber: number;
      reason: string;
      locator: EvidenceLocator;
    }
  | { kind: 'REFUSED'; reason: string; locator: EvidenceLocator };

/** Clamp to [0,1] and order the corners; a degenerate box is not a box. */
export function normalizeRect(rect: readonly number[]): RectNorm | null {
  if (rect.length !== 4) return null;
  if (!rect.every((value) => Number.isFinite(value))) return null;
  const x0 = Math.min(Math.max(rect[0] as number, 0), 1);
  const y0 = Math.min(Math.max(rect[1] as number, 0), 1);
  const x1 = Math.min(Math.max(rect[2] as number, 0), 1);
  const y1 = Math.min(Math.max(rect[3] as number, 0), 1);
  if (x1 <= x0 || y1 <= y0) return null;
  return { x0, y0, x1, y1 };
}

/**
 * Rotate a normalized rectangle by the given rotation around the page centre.
 * All four corners are transformed, then the axis-aligned bounds are taken.
 */
export function rotateRect(rect: RectNorm, rotation: Rotation): RectNorm {
  const corners: Array<[number, number]> = [
    [rect.x0, rect.y0],
    [rect.x1, rect.y0],
    [rect.x1, rect.y1],
    [rect.x0, rect.y1],
  ];
  const rotated = corners.map(([x, y]) => {
    switch (rotation) {
      case 90:
        return [1 - y, x] as [number, number];
      case 180:
        return [1 - x, 1 - y] as [number, number];
      case 270:
        return [y, 1 - x] as [number, number];
      default:
        return [x, y] as [number, number];
    }
  });
  const xs = rotated.map(([x]) => x);
  const ys = rotated.map(([, y]) => y);
  return {
    x0: Math.min(...xs),
    y0: Math.min(...ys),
    x1: Math.max(...xs),
    y1: Math.max(...ys),
  };
}

/** Displayed page size after the user's extra rotation (90/270 swap the axes). */
export function rotatedPageSize(
  box: PageBox,
  rotation: Rotation,
): { width: number; height: number } {
  const swaps = rotation === 90 || rotation === 270;
  return swaps
    ? { width: box.displayHeight, height: box.displayWidth }
    : { width: box.displayWidth, height: box.displayHeight };
}

export interface PlacementOptions {
  /** The CSS pixel size the page currently occupies on screen. */
  cssWidth: number;
  cssHeight: number;
  /** Extra rotation applied by the reader (NOT the PDF's built-in rotation). */
  userRotation?: Rotation;
  /**
   * Device pixel ratio — accepted ONLY so callers can pass the same number used
   * for the canvas; it deliberately does not affect the overlay geometry.
   */
  devicePixelRatio?: number;
}

export function placeRect(rect: RectNorm, options: PlacementOptions): CssRect {
  const rotation = options.userRotation ?? 0;
  const size = rotatedPageSize(
    {
      pageNumber: 0,
      displayWidth: options.cssWidth,
      displayHeight: options.cssHeight,
      referenceRotation: 0,
    },
    rotation,
  );
  const normalized = rotateRect(rect, rotation);
  return {
    left: normalized.x0 * size.width,
    top: normalized.y0 * size.height,
    width: (normalized.x1 - normalized.x0) * size.width,
    height: (normalized.y1 - normalized.y0) * size.height,
  };
}

/**
 * Decide what the reader may draw for one locator.
 *
 * Refusals are first-class: a locator whose document hash does not match the
 * opened document, or that belongs to another version, must NOT be drawn on this
 * page (docs/07 §1).
 */
export function locate(
  locator: EvidenceLocator,
  page: PageEvidence,
  options: PlacementOptions,
): LocatorOutcome {
  if (locator.paper_version_id !== page.paper_version_id) {
    return {
      kind: 'REFUSED',
      reason: '该证据属于另一个版本，不在当前页面画框',
      locator,
    };
  }
  if (locator.document_sha256 !== page.document_sha256) {
    return {
      kind: 'REFUSED',
      reason: '证据的原文哈希与当前打开的原文不一致',
      locator,
    };
  }
  if (locator.page_number !== page.page_number) {
    return {
      kind: 'REFUSED',
      reason: `该证据位于第 ${locator.page_number} 页`,
      locator,
    };
  }
  const rect = locator.rect_norm ? normalizeRect(locator.rect_norm) : null;
  if (locator.precision !== 'REGION' || rect === null) {
    return {
      kind: 'PAGE_ONLY',
      pageNumber: locator.page_number,
      // The reason comes from the server when it has one; the UI never invents a
      // box to hide a missing geometry.
      reason: locator.reason ?? (rect === null ? '缺少可用的区域坐标' : '仅能定位到页面'),
      locator,
    };
  }
  return { kind: 'BOX', rect: placeRect(rect, options), locator };
}

/** Page matcher: the reader may only draw a locator on the page it names. */
export function locatorsForPage(page: PageEvidence, evidence: EvidenceLocator[]): EvidenceLocator[] {
  return evidence.filter((locator) => locator.page_number === page.page_number);
}
