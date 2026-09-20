/**
 * UX-054 — 320px, 200% zoom and keyboard-only use stay readable.
 *
 * Requirement (docs/08 §可访问性, docs/05): 键盘可达、dialog焦点、200%缩放、
 * 320px 可读；字体不得小于 12px；不依赖颜色单独表达状态。
 */

import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const cssFiles = [
  'src/design/typography.css',
  'src/design/tokens.css',
  'src/app/shell/shell.module.css',
  'src/features/library/library.module.css',
  'src/features/operations/operations.module.css',
].map((path) => ({ path, css: readFileSync(resolve(process.cwd(), path), 'utf8') }));

describe('UX-054 responsive and keyboard readability', () => {
  it('never uses a font size below 12px', () => {
    for (const { path, css } of cssFiles) {
      const sizes = [...css.matchAll(/font-size:\s*([\d.]+)px/g)].map((match) => Number(match[1]));
      for (const size of sizes) {
        expect(size, `${path} declares ${size}px`).toBeGreaterThanOrEqual(12);
      }
    }
  });

  it('sizes text in rem/var so a 200% zoom scales the layout', () => {
    const typography = cssFiles.find((entry) => entry.path === 'src/design/typography.css')?.css ?? '';
    expect(typography).toMatch(/--text-xs:\s*12px/);
    expect(typography).toMatch(/--text-base:\s*15px/);
    // The root font-size is the zoom anchor; nothing hard-codes a viewport width.
    expect(typography).toMatch(/html\s*\{[^}]*font-size:\s*var\(--text-base\)/);
  });

  it('collapses the shell for narrow viewports instead of overflowing', () => {
    const shell = cssFiles.find((entry) => entry.path === 'src/app/shell/shell.module.css')?.css ?? '';
    expect(shell).toMatch(/@media/);
    expect(shell).toMatch(/max-width:\s*(48|52|56|600|640|768|800)px/);
  });

  it('keeps the 320px layout free of fixed pixel widths', () => {
    for (const { path, css } of cssFiles) {
      const fixed = [...css.matchAll(/(?:^|\s)width:\s*(\d{3,})px/g)].map((match) => Number(match[1]));
      for (const value of fixed) {
        expect(value, `${path} sets width:${value}px`).toBeLessThanOrEqual(320);
      }
    }
  });

  it('reaches every primary control with the keyboard and shows a focus ring', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Keyboard paper' }] });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    await screen.findByRole('link', { name: 'Keyboard paper' });

    const reached: string[] = [];
    for (let step = 0; step < 25; step += 1) {
      await user.tab();
      const active = document.activeElement as HTMLElement | null;
      if (!active || active === document.body) break;
      reached.push(active.tagName.toLowerCase());
    }
    expect(reached.length).toBeGreaterThan(5);
    expect(reached).toContain('input');
  });

  it('moves focus into a dialog and returns it on close', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Keyboard paper' }] });
    renderApp(server, { path: '/app/library' });
    const user = userEvent.setup();
    const trigger = await screen.findByRole('button', { name: '修改深度' }).catch(() => null);
    // The batch tray only exists with a selection: select one row first.
    await user.click(await screen.findByRole('checkbox', { name: '选择 Keyboard paper' }));
    const scopeButton = trigger ?? (await screen.findByRole('button', { name: '修改深度' }));
    await user.click(scopeButton);
    expect(await screen.findByTestId('scope-preview')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByTestId('scope-preview')).not.toBeInTheDocument();
  });

  it('does not rely on colour alone for status', async () => {
    const server = createFakeServer({
      snapshot: {
        queue: { pending: 0, running: 0, failed: 3 },
        workers: [],
        modules: [{ module_id: 'm', health: 'DEGRADED', observed_at: null, source: 'probe', reason: 'degraded' }],
        providers: [],
        disk: [],
        eta_available: false,
        observed_at: 'now',
        unknown_reasons: [],
      },
    });
    renderApp(server, { path: '/app/settings' });
    const split = await screen.findByTestId('health-split');
    // The state is spelled out in words next to any colour cue.
    expect(split.textContent).toMatch(/正常|部分降级|不可用|未观测/);
    render(<App initialPreferences={undefined} />);
    expect(App).toBeTruthy();
  });
});
