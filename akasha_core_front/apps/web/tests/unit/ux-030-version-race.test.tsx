/**
 * UX-030 — switching versions never mixes evidence between documents.
 *
 * Requirement (docs/07 §1/§3): 每次打开阅读器固定 paper_id + paper_version_id +
 * document_sha256；claim与evidence分别验证属于该版本；取消切换前的RenderTask；避免旧请求
 * 完成后覆盖新paper/version画布.
 */

import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';
import { createFakeEngine } from '../support/fake-engine';
import { locate } from '../../src/reader/geometry';
import type { EvidenceLocator, PageEvidence } from '../../src/api/contract';

const HASH_A = 'a'.repeat(64);
const HASH_B = 'b'.repeat(64);

function locator(overrides: Partial<EvidenceLocator> = {}): EvidenceLocator {
  return {
    evidence_id: 'ev_a',
    paper_version_id: 'pver_v1',
    document_sha256: HASH_A,
    page_number: 1,
    page_label: null,
    coordinate_space: 'DISPLAY_NORMALIZED_V1',
    rect_norm: [0.1, 0.1, 0.4, 0.4],
    precision: 'REGION',
    source_method: 'PDF_NATIVE',
    ocr_confidence: null,
    transform_revision: 'rev-1',
    extraction_run_id: 'run_1',
    reason: null,
    ...overrides,
  };
}

function pageEvidence(overrides: Partial<PageEvidence> = {}): PageEvidence {
  return {
    paper_version_id: 'pver_v1',
    document_sha256: HASH_A,
    page_number: 1,
    page_display_width: 600,
    page_display_height: 800,
    reference_rotation: 0,
    evidence: [],
    ...overrides,
  };
}

describe('UX-030 version switch cannot bleed evidence', () => {
  it('shows the later evidence page, not its cached neighbour, without reopening the PDF', async () => {
    const engine = createFakeEngine(5);
    const factory = vi.fn(async () => engine);
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper', paper_version_id: 'pver_v1' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
      claimEvidence: { clm_a: [{ evidence_id: 'ev_a', paper_version_id: 'pver_v1', page_start: 3, text: 'original quote' }] },
      pages: { 3: { ...pageEvidence({ page_number: 3, page_display_width: 0, page_display_height: 0 }),
        evidence: [{ ...locator({ page_number: 3, precision: 'PAGE', rect_norm: null }) }] } },
    });
    renderApp(server, { path: '/app/papers/pap_1?paper_version_id=pver_v1', engineFactory: factory });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '证据（1）' }));
    await waitFor(() => expect(screen.getByLabelText('页码')).toHaveValue(3));
    await waitFor(() => expect(screen.getByTestId('canvas-area').querySelector('[data-page="3"] canvas')).not.toBeNull());
    expect(screen.getByTestId('canvas-area').querySelectorAll('[data-page]')).toHaveLength(1);
    expect(screen.getByTestId('canvas-area').querySelector('canvas')).toHaveStyle({ width: '612px', height: '792px' });
    expect(screen.getByTestId('inspector-quote')).toHaveTextContent('original quote');
    await user.click(screen.getByRole('button', { name: '下一页' }));
    await waitFor(() => expect(screen.getByTestId('canvas-area').querySelector('[data-page="4"] canvas')).not.toBeNull());
    expect(factory).toHaveBeenCalledTimes(1);
    expect(engine.destroyed).toBe(false);
  });

  it('refuses a locator that belongs to the previously opened version', () => {
    const outcome = locate(
      locator({ paper_version_id: 'pver_v1', document_sha256: HASH_A }),
      pageEvidence({ paper_version_id: 'pver_v2', document_sha256: HASH_B }),
      { cssWidth: 600, cssHeight: 800 },
    );
    expect(outcome.kind).toBe('REFUSED');
  });

  it('refuses a locator whose document hash no longer matches', () => {
    const outcome = locate(
      locator({ document_sha256: HASH_A }),
      pageEvidence({ document_sha256: HASH_B }),
      { cssWidth: 600, cssHeight: 800 },
    );
    expect(outcome.kind).toBe('REFUSED');
    if (outcome.kind === 'REFUSED') expect(outcome.reason).toContain('哈希');
  });

  it('does not jump to a newer version by itself and announces it instead', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Two-version paper', paper_version_id: 'pver_v1' }],
      versions: [
        { id: 'pver_v2', label: 'v2', is_latest: true },
        { id: 'pver_v1', label: 'v1', is_latest: false },
      ],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
    });
    renderApp(server, { path: '/app/papers/pap_1?paper_version_id=pver_v1' });
    await waitForWorkspace();

    // Still on the pinned version; the newer one is only announced.
    expect(screen.getByTestId('pinned-version')).toHaveTextContent('pver_v1');
    const notice = await screen.findByTestId('newer-version-notice');
    expect(notice).toHaveTextContent('pver_v2');
    expect(screen.getByTestId('pinned-version')).toHaveTextContent('pver_v1');

    // Every workspace read was scoped to the pinned version.
    const scoped = server.requests.filter((request) => request.path.includes('/workspace?'));
    expect(scoped.every((request) => request.path.includes('paper_version_id=pver_v1'))).toBe(true);
  });

  it('switching versions cancels in-flight renders for the old document', async () => {
    const engine = createFakeEngine(3);
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Two-version paper', paper_version_id: 'pver_v1' }],
      versions: [
        { id: 'pver_v2', label: 'v2', is_latest: true },
        { id: 'pver_v1', label: 'v1', is_latest: false },
      ],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
    });
    renderApp(server, {
      path: '/app/papers/pap_1?paper_version_id=pver_v1',
      engineFactory: async () => engine,
    });
    await waitForWorkspace();

    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: '原文' }));
    await screen.findByTestId('reader-panel');
    await waitFor(() => expect(engine.renderedPages.length).toBeGreaterThan(0));
    await waitFor(() => expect(screen.getByTestId('canvas-area').querySelector('canvas')).not.toBeNull());

    // Switch version: the reader must tear the old document down.
    await user.click(screen.getByRole('button', { name: /切换到新版本/ }));
    await waitFor(() => expect(window.location.search).toContain('paper_version_id=pver_v2'));
    await waitFor(() => expect(engine.destroyed).toBe(true), { timeout: 3000 });

    // A render that completes after the switch must not be painted: the panel
    // reset its rendered list when the document changed.
    expect(screen.queryAllByTestId('evidence-box')).toHaveLength(0);
  });

  it('keeps the page-evidence request scoped to the pinned version', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper', paper_version_id: 'pver_v1' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
      pages: {
        1: {
          paper_version_id: 'pver_v1',
          document_sha256: HASH_A,
          page_number: 1,
          page_display_width: 600,
          page_display_height: 800,
          evidence: [locator() as unknown as Record<string, unknown>],
        },
      },
      claimEvidence: { clm_a: [{ evidence_id: 'ev_a', page_start: 1, text: 'quote' }] },
    });
    renderApp(server, { path: '/app/papers/pap_1?paper_version_id=pver_v1' });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '证据（1）' }));
    await screen.findByTestId('reader-panel');

    const requests = server.requests.filter((request) => request.path.includes('/pages/'));
    expect(requests.length).toBeGreaterThan(0);
    expect(
      requests.every((request) => request.path.includes('/paper-versions/pver_v1/')),
    ).toBe(true);
  });
});
