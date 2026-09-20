/**
 * UX-028 — the coordinate matrix (docs/07 §2 + §6).
 *
 * Fixtures with KNOWN geometry: native single/double column, scanned OCR,
 * partial OCR, a non-zero crop box, all four rotations, 2× device pixel ratio,
 * 125%/200% browser zoom, an unusually long page and mixed page sizes.
 *
 * Tolerances from docs/07 §6: region edges within 2 CSS px at 1366×900 / 100%
 * zoom, and normalized error ≤ 0.005 across zoom levels.
 */

import { describe, expect, it } from 'vitest';
import type { EvidenceLocator, PageEvidence } from '../../src/api/contract';
import { locate, normalizeRect, placeRect, rotateRect, rotatedPageSize } from '../../src/reader/geometry';

const BASE = { width: 1366, height: 900 };

function locator(overrides: Partial<EvidenceLocator> = {}): EvidenceLocator {
  return {
    evidence_id: 'ev_1',
    paper_version_id: 'pver_1',
    document_sha256: 'a'.repeat(64),
    page_number: 1,
    page_label: '5',
    coordinate_space: 'DISPLAY_NORMALIZED_V1',
    rect_norm: [0.1, 0.2, 0.3, 0.4],
    precision: 'REGION',
    source_method: 'PDF_NATIVE',
    ocr_confidence: null,
    transform_revision: 'rev-1',
    extraction_run_id: 'run_1',
    reason: null,
    ...overrides,
  };
}

function page(overrides: Partial<PageEvidence> = {}): PageEvidence {
  return {
    paper_version_id: 'pver_1',
    document_sha256: 'a'.repeat(64),
    page_number: 1,
    page_display_width: BASE.width,
    page_display_height: BASE.height,
    reference_rotation: 0,
    evidence: [],
    ...overrides,
  };
}

function boxFor(rect: readonly number[], width: number, height: number, rotation: 0 | 90 | 180 | 270 = 0) {
  const normalized = normalizeRect(rect);
  if (!normalized) throw new Error('fixture rect is degenerate');
  return placeRect(normalized, { cssWidth: width, cssHeight: height, userRotation: rotation });
}

describe('UX-028 coordinate matrix', () => {
  it('places a native single-column region at the expected CSS pixels', () => {
    // 1366×900 page, region from (10%,20%) to (30%,40%).
    const box = boxFor([0.1, 0.2, 0.3, 0.4], BASE.width, BASE.height);
    expect(box.left).toBeCloseTo(136.6, 3);
    expect(box.top).toBeCloseTo(180, 3);
    expect(box.width).toBeCloseTo(273.2, 3);
    expect(box.height).toBeCloseTo(180, 3);
    // Edge tolerance for the golden fixture: ≤2 CSS px.
    expect(Math.abs(box.left - 136.6)).toBeLessThanOrEqual(2);
    expect(Math.abs(box.width - 273.2)).toBeLessThanOrEqual(2);
  });

  it('keeps a two-column region inside its own column', () => {
    const left = boxFor([0.08, 0.1, 0.46, 0.9], BASE.width, BASE.height);
    const right = boxFor([0.54, 0.1, 0.92, 0.9], BASE.width, BASE.height);
    expect(left.left + left.width).toBeLessThan(right.left);
    expect(right.left + right.width).toBeLessThanOrEqual(BASE.width);
  });

  it('transforms all four corners for every rotation', () => {
    const rect = normalizeRect([0.1, 0.2, 0.3, 0.4]);
    expect(rect).not.toBeNull();
    expect(rotateRect(rect!, 0)).toEqual({ x0: 0.1, y0: 0.2, x1: 0.3, y1: 0.4 });
    // 90°: (x,y) → (1-y, x)
    expect(rotateRect(rect!, 90)).toEqual({ x0: 0.6, y0: 0.1, x1: 0.8, y1: 0.3 });
    expect(rotateRect(rect!, 180)).toEqual({ x0: 0.7, y0: 0.6, x1: 0.9, y1: 0.8 });
    expect(rotateRect(rect!, 270)).toEqual({ x0: 0.2, y0: 0.7, x1: 0.4, y1: 0.9 });
  });

  it('swaps the page axes for 90/270 and keeps them for 0/180', () => {
    const pageBox = {
      pageNumber: 1,
      displayWidth: 1366,
      displayHeight: 900,
      referenceRotation: 0 as const,
    };
    expect(rotatedPageSize(pageBox, 90)).toEqual({ width: 900, height: 1366 });
    expect(rotatedPageSize(pageBox, 270)).toEqual({ width: 900, height: 1366 });
    expect(rotatedPageSize(pageBox, 180)).toEqual({ width: 1366, height: 900 });
  });

  it('never applies devicePixelRatio to the overlay', () => {
    const normalized = normalizeRect([0.1, 0.2, 0.3, 0.4])!;
    const at1x = placeRect(normalized, {
      cssWidth: BASE.width,
      cssHeight: BASE.height,
      devicePixelRatio: 1,
    });
    const at2x = placeRect(normalized, {
      cssWidth: BASE.width,
      cssHeight: BASE.height,
      devicePixelRatio: 2,
    });
    expect(at2x).toEqual(at1x);
  });

  it('scales with browser zoom while keeping the normalized position (≤0.005)', () => {
    const normalized = normalizeRect([0.1, 0.2, 0.3, 0.4])!;
    for (const zoom of [1, 1.25, 2]) {
      const box = placeRect(normalized, {
        cssWidth: BASE.width * zoom,
        cssHeight: BASE.height * zoom,
      });
      const backToNormalized = box.left / (BASE.width * zoom);
      expect(Math.abs(backToNormalized - 0.1)).toBeLessThanOrEqual(0.005);
      expect(Math.abs(box.width / (BASE.width * zoom) - 0.2)).toBeLessThanOrEqual(0.005);
    }
  });

  it('uses the crop-adjusted display size, so a non-zero crop box needs no special case', () => {
    // Same normalized region on a cropped page (display 1000×500 after crop).
    const box = boxFor([0.1, 0.2, 0.3, 0.4], 1000, 500);
    expect(box.left).toBeCloseTo(100, 6);
    expect(box.top).toBeCloseTo(100, 6);
    expect(box.width).toBeCloseTo(200, 6);
    expect(box.height).toBeCloseTo(100, 6);
  });

  it('handles a scanned OCR region and a partial-OCR region with the same normalization', () => {
    const scanned = locate(
      locator({ source_method: 'OCR', ocr_confidence: 0.91, rect_norm: [0.2, 0.25, 0.5, 0.45] }),
      page({ evidence: [] }),
      { cssWidth: BASE.width, cssHeight: BASE.height },
    );
    expect(scanned.kind).toBe('BOX');
    if (scanned.kind === 'BOX') expect(scanned.rect.left).toBeCloseTo(273.2, 3);

    const partial = locate(
      locator({
        source_method: 'OCR_PARTIAL',
        precision: 'REGION',
        rect_norm: [0.5, 0.5, 0.9, 0.6],
      }),
      page({ evidence: [] }),
      { cssWidth: BASE.width, cssHeight: BASE.height },
    );
    expect(partial.kind).toBe('BOX');
  });

  it('supports an unusually long page and mixed page sizes', () => {
    const long = boxFor([0.1, 0.9, 0.2, 0.98], 800, 6000);
    expect(long.top).toBeCloseTo(5400, 3);
    expect(long.height).toBeCloseTo(480, 3);
    const small = boxFor([0.1, 0.9, 0.2, 0.98], 400, 300);
    expect(small.top).toBeCloseTo(270, 3);
  });

  it('refuses a rect outside the page instead of drawing off-page', () => {
    expect(normalizeRect([0.9, 0.9, 1.4, 1.2])).toEqual({ x0: 0.9, y0: 0.9, x1: 1, y1: 1 });
    expect(normalizeRect([0.5, 0.5, 0.5, 0.6])).toBeNull(); // zero width
    expect(normalizeRect([0.2, 0.2, 0.1, 0.3])).toBeNull(); // inverted
    expect(normalizeRect([0.1, 0.2, 0.3, Number.NaN])).toBeNull();
  });

  it('refuses locators from another version or another document hash', () => {
    const foreign = locate(
      locator({ paper_version_id: 'pver_other' }),
      page(),
      { cssWidth: BASE.width, cssHeight: BASE.height },
    );
    expect(foreign.kind).toBe('REFUSED');

    const otherHash = locate(
      locator({ document_sha256: 'b'.repeat(64) }),
      page(),
      { cssWidth: BASE.width, cssHeight: BASE.height },
    );
    expect(otherHash.kind).toBe('REFUSED');

    const otherPage = locate(
      locator({ page_number: 4 }),
      page({ page_number: 1 }),
      { cssWidth: BASE.width, cssHeight: BASE.height },
    );
    expect(otherPage.kind).toBe('REFUSED');
    if (otherPage.kind === 'REFUSED') expect(otherPage.reason).toContain('第 4 页');
  });
});
