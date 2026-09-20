/**
 * UX-040 — cross-protocol comparison blocks improper quantification.
 * UX-041 — missing values are never rendered as 0.
 *
 * Requirement (docs/03 §S09 + docs/05 §缺失值): 量化图仅在 protocol_key相同 时开启；
 * 不同硬件latency、不同budget结果默认分组并标"不能直接比较"；不计算伪提升；缺失与不适用
 * 分开.
 */

import { screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import { cellText } from '../../src/features/compare/ComparePage';

function comparison(cells: Record<string, unknown>[], versions = ['pver_a', 'pver_b']) {
  return {
    paper_version_ids: versions,
    selection_hash: 'sel-1',
    analysis_revision: 'rev-1',
    cells,
    papers: Object.fromEntries(
      versions.map((id) => [
        id,
        {
          paper_id: `pap_${id}`,
          version_label: 'v1',
          document_sha256: 'e'.repeat(64),
          publication_date: '2023-01-01',
        },
      ]),
    ),
  };
}

function cell(versionId: string, dimension: string, overrides: Record<string, unknown> = {}) {
  return {
    paper_version_id: versionId,
    dimension,
    value: 'stored statement',
    unit: null,
    protocol_key: 'dataset:coco|split:val|metric:mAP',
    comparable: true,
    comparability_reason: null,
    source_refs: [],
    missing_reason: null,
    ...overrides,
  };
}

describe('UX-040 cross-protocol comparison refuses to quantify', () => {
  it('marks cells as not comparable when the protocol keys differ', async () => {
    const server = createFakeServer({
      comparison: comparison([
        cell('pver_a', 'evidence'),
        cell('pver_b', 'evidence', {
          protocol_key: 'dataset:voc|split:test|metric:mAP',
          comparable: false,
          comparability_reason: '不同评估协议（dataset/split/metric/单位不同），未合并计算',
        }),
      ]),
    });
    renderApp(server, { path: '/app/compare?versions=pver_a,pver_b' });

    const matrix = await screen.findByTestId('compare-matrix');
    expect(matrix).toHaveTextContent('不能直接比较'.slice(0, 4));
    const reasons = screen.getAllByTestId('not-comparable');
    expect(reasons.length).toBeGreaterThan(0);
    expect(reasons[0]).toHaveTextContent('未合并计算');
  });

  it('never prints a computed improvement or a winner', async () => {
    const server = createFakeServer({
      comparison: comparison([
        cell('pver_a', 'evidence'),
        cell('pver_b', 'evidence', { comparable: false, comparability_reason: '不同协议' }),
      ]),
    });
    renderApp(server, { path: '/app/compare?versions=pver_a,pver_b' });
    const matrix = await screen.findByTestId('compare-matrix');
    expect(matrix.textContent ?? '').not.toMatch(/提升\s*[\d.]+%|更优|胜出|best/i);
  });

  it('shows each version with its id and date', async () => {
    const server = createFakeServer({ comparison: comparison([cell('pver_a', 'method')]) });
    renderApp(server, { path: '/app/compare?versions=pver_a,pver_b' });
    const matrix = await screen.findByTestId('compare-matrix');
    expect(matrix).toHaveTextContent('pver_a');
    expect(matrix).toHaveTextContent('pver_b');
    expect(matrix).toHaveTextContent('2023-01-01');
  });

  it('requires at least two papers and says so', async () => {
    const server = createFakeServer({});
    renderApp(server, { path: '/app/compare?versions=pver_a' });
    expect(await screen.findByTestId('compare-needs-two')).toHaveTextContent('至少选择 2 篇');
    expect(screen.queryByTestId('compare-matrix')).not.toBeInTheDocument();
  });

  it('carries the selection hash so an export can be traced', async () => {
    const server = createFakeServer({ comparison: comparison([cell('pver_a', 'method')]) });
    renderApp(server, { path: '/app/compare?versions=pver_a,pver_b' });
    await waitFor(() =>
      expect(screen.getByTestId('selection-hash')).toHaveTextContent('sel-1'),
    );
  });
});

describe('UX-041 missing values are not zero', () => {
  it('renders the missing reason instead of 0', () => {
    const cells = [
      {
        paper_version_id: 'pver_a',
        dimension: 'cost',
        value: null,
        missing_reason: 'NOT_REPORTED',
        comparable: false,
      },
      {
        paper_version_id: 'pver_b',
        dimension: 'cost',
        value: null,
        missing_reason: 'NOT_APPLICABLE',
        comparable: false,
      },
    ];
    expect(cellText(cells, 'pver_a', 'cost')).toBe('未报告');
    expect(cellText(cells, 'pver_b', 'cost')).toBe('不适用');
    expect(cellText(cells, 'pver_a', 'cost')).not.toBe('0');
  });

  it('separates "not reported" from "not applicable"', () => {
    expect(cellText(undefined, 'pver_a', 'cost')).toBe('未知');
    expect(
      cellText(
        [
          {
            paper_version_id: 'pver_a',
            dimension: 'cost',
            value: null,
            missing_reason: null,
            comparable: false,
          },
        ],
        'pver_a',
        'cost',
      ),
    ).toBe('未报告');
  });

  it('shows a stored value verbatim, without rounding', () => {
    expect(
      cellText(
        [
          {
            paper_version_id: 'pver_a',
            dimension: 'evidence',
            value: 'mAP 43.4%（+2.1 个百分点）',
            missing_reason: null,
            comparable: true,
          },
        ],
        'pver_a',
        'evidence',
      ),
    ).toBe('mAP 43.4%（+2.1 个百分点）');
  });

  it('renders missing cells in the matrix as their reason', async () => {
    const server = createFakeServer({
      comparison: comparison([
        cell('pver_a', 'dataset', { value: null, missing_reason: 'NOT_EXTRACTED', comparable: false }),
        cell('pver_b', 'dataset', { value: null, missing_reason: 'NOT_APPLICABLE', comparable: false }),
      ]),
    });
    renderApp(server, { path: '/app/compare?versions=pver_a,pver_b' });
    const matrix = await screen.findByTestId('compare-matrix');
    expect(matrix).toHaveTextContent('未提取');
    expect(matrix).toHaveTextContent('不适用');
  });
});
