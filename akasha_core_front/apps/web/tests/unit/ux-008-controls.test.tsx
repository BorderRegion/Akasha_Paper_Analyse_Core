import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Button } from '../../src/components/ui/Button';
import { IconButton } from '../../src/components/ui/IconButton';
import { Tag } from '../../src/components/ui/Tag';
import { MIN_TARGET_PX } from '../../src/lib/a11y';

const css = readFileSync(resolve(process.cwd(), 'src/components/ui/ui.module.css'), 'utf8');

describe('UX-008 control focus, labels and hit areas', () => {
  it('uses one focus-visible treatment for every interactive control', () => {
    // The focus rule is a single grouped selector list followed by one body.
    const match = /((?:\.[a-zA-Z]+:focus-visible,\s*)+\.[a-zA-Z]+:focus-visible)\s*{([^}]*)}/s.exec(css);
    expect(match, 'grouped :focus-visible rule missing').not.toBeNull();
    const [, selectors, body] = match as RegExpExecArray;
    for (const selector of ['.button', '.iconButton', '.tag', '.navLink', '.statusPill', '.input']) {
      expect(selectors, `${selector} is not covered by the shared focus rule`).toContain(selector);
    }
    expect(body).toContain('outline: 2px solid var(--accent)');
    expect(body).toContain('outline-offset: 2px');
  });

  it('keeps every declared target at or above the minimum size', () => {
    const minHeight = [...css.matchAll(/min-height:\s*(\d+)px/g)].map((m) => Number(m[1]));
    expect(minHeight.length).toBeGreaterThan(0);
    // Icon buttons are square; check the declared width too.
    const width = Number(/\.iconButton\s*{[^}]*width:\s*(\d+)px/s.exec(css)?.[1] ?? 0);
    const height = Number(/\.iconButton\s*{[^}]*height:\s*(\d+)px/s.exec(css)?.[1] ?? 0);
    expect(Math.min(width, height)).toBeGreaterThanOrEqual(MIN_TARGET_PX);
  });

  it('gives an icon-only control an accessible name', async () => {
    render(<IconButton label="刷新数据" icon={<span aria-hidden="true">⟳</span>} />);
    const button = screen.getByRole('button', { name: '刷新数据' });
    expect(button).toHaveAttribute('title', '刷新数据');
    await userEvent.tab();
    expect(button).toHaveFocus();
  });

  it('renders text buttons with their visible label as the accessible name', () => {
    render(<Button variant="primary">导入 PDF</Button>);
    expect(screen.getByRole('button', { name: '导入 PDF' })).toBeEnabled();
  });

  it('marks candidate tags instead of promoting them', () => {
    render(<Tag label="vision-transformer" namespace="architecture" candidate />);
    expect(screen.getByText(/候选 · vision-transformer/)).toBeInTheDocument();
  });

  it('declares the control chrome once for every control family', () => {
    // One shared block gives controls their font, radius, border, surface and
    // colour; per-control blocks then only adjust padding/size. This is what
    // keeps focus, target size and tone from drifting page by page.
    const shared = /((?:\.[a-zA-Z]+,\s*)+\.[a-zA-Z]+)\s*{([^}]*)}/s.exec(css);
    expect(shared, 'shared control block missing').not.toBeNull();
    const [, selectors, body] = shared as RegExpExecArray;
    for (const selector of ['.button', '.iconButton', '.tag', '.statusPill', '.navLink']) {
      expect(selectors).toContain(selector);
    }
    expect(body).toContain('border: 1px solid var(--control-border)');
    expect(body).toContain('background: var(--surface)');
    expect(body).toContain('color: var(--text)');
    expect(body).toContain('border-radius: 10px');

    // Inputs use the same tokens even though they are not buttons.
    const input = /\.input\s*{([^}]*)}/s.exec(css)?.[1] ?? '';
    expect(input).toContain('var(--control-border)');
    expect(input).toContain('var(--surface)');
  });
});
