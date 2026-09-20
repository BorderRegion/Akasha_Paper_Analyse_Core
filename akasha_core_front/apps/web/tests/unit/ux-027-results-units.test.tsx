/**
 * UX-027 — units, footnotes and missing values stay accurate.
 *
 * Requirement (docs/03 §S04 + docs/07 §4): 表格里单位、误差项、脚注、行列标题必须保留；
 * "+2.1个百分点"和"相对提升2.1%"分开；数值精度保持原文；不能用前端round将矛盾抹去；
 * 缺失方差显示"未报告"，不是0.
 *
 * The backend stores claim statements as TEXT and exposes no structured
 * value/unit cell, so this view renders the stored statement verbatim and marks
 * the structured cells as not structured. Parsing numbers out of prose would
 * invent precision the system does not have.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ResultsSection, collectFootnotes } from '../../src/features/paper/PaperWorkspacePage';
import type { ClaimPreview } from '../../src/api/contract';

function claim(id: string, statement: string): ClaimPreview {
  return {
    claim_id: id,
    statement,
    claim_type: 'FACT',
    support_state: 'SUPPORTED',
    paper_version_id: 'pver_1',
    evidence_count: 2,
    is_superseded: false,
  };
}

const STATEMENTS = [
  'mAP 从 41.3% 提升到 43.4%（+2.1 个百分点）†',
  '相对提升 2.1%（相对基线）',
  '延迟从 12.4 ms 降到 9.87 ms（未报告方差）',
  '在 8×A100 上训练 300 epoch*',
];

describe('UX-027 results keep units, footnotes and missing values honest', () => {
  it('renders every stored statement verbatim, without rounding', () => {
    render(<ResultsSection claims={STATEMENTS.map((text, index) => claim(`clm_${index}`, text))} onOpenEvidence={() => undefined} />);
    for (const statement of STATEMENTS) {
      expect(screen.getByText(statement)).toBeInTheDocument();
    }
    // No rounded or converted variant appears anywhere in the view.
    expect(screen.queryByText(/2\.10 个百分点|43%|9\.9 ms/)).not.toBeInTheDocument();
  });

  it('keeps percentage points and relative percentages apart', () => {
    const { container } = render(
      <ResultsSection
        claims={[claim('clm_1', '+2.1 个百分点'), claim('clm_2', '相对提升 2.1%')]}
        onOpenEvidence={() => undefined}
      />,
    );
    expect(container.textContent).toContain('+2.1 个百分点');
    expect(container.textContent).toContain('相对提升 2.1%');
    // The UI never rewrites one into the other.
    expect(container.textContent).not.toContain('+2.1%');
  });

  it('shows 未报告/未结构化 instead of 0 for data the server did not provide', () => {
    render(<ResultsSection claims={[claim('clm_1', STATEMENTS[2] as string)]} onOpenEvidence={() => undefined} />);
    expect(screen.getByTestId('unit-cell')).toHaveTextContent('未结构化');
    expect(screen.getByTestId('variance-cell')).toHaveTextContent('未报告');
    expect(screen.getByTestId('variance-cell')).not.toHaveTextContent('0');
  });

  it('preserves footnote markers and lists them', () => {
    render(<ResultsSection claims={STATEMENTS.map((text, index) => claim(`clm_${index}`, text))} onOpenEvidence={() => undefined} />);
    const footnotes = screen.getByTestId('footnotes');
    expect(footnotes).toHaveTextContent('†');
    expect(footnotes).toHaveTextContent('*');
    expect(collectFootnotes(STATEMENTS).sort()).toEqual(['*', '†']);
  });

  it('states the limitation instead of pretending to structured numbers', () => {
    render(<ResultsSection claims={[claim('clm_1', STATEMENTS[0] as string)]} onOpenEvidence={() => undefined} />);
    expect(
      screen.getByText(/单位和误差尚未单独整理/),
    ).toBeInTheDocument();
    expect(screen.getByText(/保留记录中的数值与精度/)).toBeInTheDocument();
  });

  it('links every row to its evidence', () => {
    const opened: string[] = [];
    render(
      <ResultsSection
        claims={[claim('clm_1', STATEMENTS[0] as string)]}
        onOpenEvidence={(item) => opened.push(item.claim_id)}
      />,
    );
    const button = screen.getByRole('button', { name: /条证据/ });
    button.click();
    expect(opened).toEqual(['clm_1']);
  });
});
