import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, beforeEach, afterEach } from 'vitest';
import {
  CONTRAST_BODY_TEXT,
  CONTRAST_LARGE_TEXT_OR_UI,
  contrastRatio,
} from '../../src/lib/a11y';
import { applyPreferences, DEFAULT_PREFERENCES, resolveTheme } from '../../src/state/theme';

const tokensCss = readFileSync(resolve(process.cwd(), 'src/design/tokens.css'), 'utf8');

function themeTokens(theme: 'light' | 'dark'): Record<string, string> {
  const selector = theme === 'light' ? ':root, [data-theme="light"]' : '[data-theme="dark"]';
  const start = tokensCss.indexOf(selector);
  expect(start, `token block for ${theme} missing`).toBeGreaterThanOrEqual(0);
  const block = tokensCss.slice(tokensCss.indexOf('{', start) + 1, tokensCss.indexOf('}', start));
  const tokens: Record<string, string> = {};
  for (const match of block.matchAll(/--([a-z-]+):\s*(#[0-9a-fA-F]{6})/g)) {
    tokens[match[1] as string] = match[2] as string;
  }
  return tokens;
}

describe('UX-007 theme tokens and contrast', () => {
  it('defines the same token set for light and dark', () => {
    const light = Object.keys(themeTokens('light')).sort();
    const dark = Object.keys(themeTokens('dark')).sort();
    expect(dark).toEqual(light);
    expect(light).toEqual(
      expect.arrayContaining([
        'background',
        'surface',
        'text',
        'muted',
        'accent',
        'accent-text',
        'border',
        'control-border',
        'success',
        'warning',
        'danger',
        'info',
      ]),
    );
  });

  it.each(['light', 'dark'] as const)('body text passes AA contrast in %s', (theme) => {
    const t = themeTokens(theme);
    for (const surface of ['background', 'surface', 'surface-soft'] as const) {
      const ratio = contrastRatio(t['text'] as string, t[surface] as string);
      expect(ratio, `text on ${surface} (${theme}) = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(
        CONTRAST_BODY_TEXT,
      );
    }
    // Secondary text must still reach the UI/large-text threshold.
    for (const surface of ['background', 'surface'] as const) {
      const ratio = contrastRatio(t['muted'] as string, t[surface] as string);
      expect(ratio, `muted on ${surface} (${theme}) = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(
        CONTRAST_LARGE_TEXT_OR_UI,
      );
    }
  });

  it.each(['light', 'dark'] as const)('status tones and primary button pass in %s', (theme) => {
    const t = themeTokens(theme);
    const pairs: Array<[string, string, number]> = [
      ['accent-text', 'accent', CONTRAST_BODY_TEXT],
      ['success', 'success-background', CONTRAST_LARGE_TEXT_OR_UI],
      ['warning', 'warning-background', CONTRAST_LARGE_TEXT_OR_UI],
      ['danger', 'danger-background', CONTRAST_LARGE_TEXT_OR_UI],
      ['info', 'info-background', CONTRAST_LARGE_TEXT_OR_UI],
    ];
    for (const [fg, bg, threshold] of pairs) {
      const ratio = contrastRatio(t[fg] as string, t[bg] as string);
      expect(ratio, `${fg} on ${bg} (${theme}) = ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(
        threshold,
      );
    }
  });

  it('applies the resolved theme to the document root', () => {
    applyPreferences({ ...DEFAULT_PREFERENCES, theme: 'dark' });
    expect(document.documentElement.dataset.theme).toBe('dark');
    applyPreferences({ ...DEFAULT_PREFERENCES, theme: 'light' });
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('resolves the system choice from the media query', () => {
    const original = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      matches: query.includes('dark'),
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      addListener: () => undefined,
      removeListener: () => undefined,
      onchange: null,
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    expect(resolveTheme('system')).toBe('dark');
    window.matchMedia = original;
  });

  beforeEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });
  afterEach(() => {
    document.documentElement.removeAttribute('data-theme');
  });
});
