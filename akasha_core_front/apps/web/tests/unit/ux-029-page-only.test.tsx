/**
 * UX-029 — a missing bbox jumps to the page and draws NO box.
 *
 * Requirement (docs/03 §S05 + docs/07 §2/§6): 若来源缺 bbox，只跳页并标"只能定位到
 * 页面"，不得画任意高亮; 无法恢复坐标应 PAGE 降级并通过诚实性测试，而不是硬凑图形测试.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import type { EvidenceLocator } from '../../src/api/contract';
import { locate } from '../../src/reader/geometry';
import { createFakeServer, type FakePageEvidenceInput } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';

const HASH = 'c'.repeat(64);

function locator(overrides: Partial<EvidenceLocator> = {}): EvidenceLocator {
  return {
    evidence_id: 'ev_1',
    paper_version_id: 'pver_pap_1',
    document_sha256: HASH,
    page_number: 1,
    page_label: '5',
    coordinate_space: 'DISPLAY_NORMALIZED_V1',
    rect_norm: null,
    precision: 'PAGE',
    source_method: 'OCR',
    ocr_confidence: 0.4,
    transform_revision: 'rev-1',
    extraction_run_id: 'run_1',
    reason: 'page geometry unavailable; only the page can be located',
    ...overrides,
  };
}

const PAGE_ONE: FakePageEvidenceInput = {
  paper_version_id: 'pver_pap_1',
  document_sha256: HASH,
  page_number: 1,
  page_display_width: 612,
  page_display_height: 792,
  evidence: [],
};

describe('UX-029 missing geometry degrades to page-only', () => {
  it('returns PAGE_ONLY with the server reason and no rectangle', () => {
    const outcome = locate(locator(), { ...PAGE_ONE, reference_rotation: 0, evidence: [] }, {
      cssWidth: 612,
      cssHeight: 792,
    });
    expect(outcome.kind).toBe('PAGE_ONLY');
    if (outcome.kind === 'PAGE_ONLY') {
      expect(outcome.pageNumber).toBe(1);
      expect(outcome.reason).toContain('page geometry unavailable');
    }
  });

  it('never fabricates a rectangle when the rect exists but precision is not REGION', () => {
    const outcome = locate(
      locator({ rect_norm: [0.1, 0.1, 0.5, 0.5], precision: 'PAGE' }),
      { ...PAGE_ONE, reference_rotation: 0, evidence: [] },
      { cssWidth: 612, cssHeight: 792 },
    );
    expect(outcome.kind).toBe('PAGE_ONLY');
  });

  it('draws no evidence box in the reader and says why', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Scanned paper' }],
      modules: [
        {
          id: 'overview',
          claims: [{ claim_id: 'clm_a', statement: '扫描件的结论', evidence_count: 1 }],
        },
      ],
      pages: {
        1: {
          ...PAGE_ONE,
          evidence: [locator() as unknown as Record<string, unknown>],
        },
      },
      claimEvidence: {
        clm_a: [
          {
            evidence_id: 'ev_1',
            paper_version_id: 'pver_pap_1',
            page_start: 1,
            text: '原文逐字引用：扫描件的准确率见表 3。',
            source_method: 'OCR',
            ocr_confidence: 0.4,
            quality_state: 'LOW_CONFIDENCE',
          },
        ],
      },
    });
    renderApp(server, { path: '/app/papers/pap_1' });
    await waitForWorkspace();

    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '证据（1）' }));

    await screen.findByTestId('reader-panel');
    // The claim's own evidence is selected automatically once the page payload
    // and the claim→evidence link have both arrived.
    await waitFor(() =>
      expect(screen.getByTestId('evidence-inspector')).toHaveTextContent('只能定位到页面'),
    );
    expect(await screen.findByText('只能定位到页面')).toBeInTheDocument();
    // No box is drawn: the geometry could not be restored.
    expect(screen.queryByTestId('evidence-box')).not.toBeInTheDocument();
    const inspector = screen.getByTestId('evidence-inspector');
    // The quote is the server's verbatim text, shown next to the honest
    // precision label.
    expect(within(inspector).getByTestId('inspector-quote')).toHaveTextContent(
      '原文逐字引用：扫描件的准确率见表 3。',
    );
  });

  it('does not draw a box for a TEXT_ONLY locator either', () => {
    const outcome = locate(
      locator({ precision: 'TEXT_ONLY', reason: 'no bounding box in the evidence' }),
      { ...PAGE_ONE, reference_rotation: 0, evidence: [] },
      { cssWidth: 612, cssHeight: 792 },
    );
    expect(outcome.kind).toBe('PAGE_ONLY');
  });

  it('reports the page it can jump to even when the reader is on another page', () => {
    const outcome = locate(
      locator({ page_number: 3 }),
      { ...PAGE_ONE, page_number: 1, reference_rotation: 0, evidence: [] },
      { cssWidth: 612, cssHeight: 792 },
    );
    // A locator for another page is REFUSED here; the reader jumps by changing
    // the page, and the box is only ever drawn on its own page.
    expect(outcome.kind).toBe('REFUSED');
  });
});
