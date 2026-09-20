import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { applyPreferences, DEFAULT_PREFERENCES, prefersReducedMotion } from '../../src/state/theme';

const motionCss = readFileSync(resolve(process.cwd(), 'src/design/motion.css'), 'utf8');
const tokensCss = readFileSync(resolve(process.cwd(), 'src/design/tokens.css'), 'utf8');

describe('UX-009 reduced motion', () => {
  it('collapses motion tokens under prefers-reduced-motion', () => {
    const block = motionCss.slice(motionCss.indexOf('@media (prefers-reduced-motion: reduce)'));
    expect(block).toContain('--motion-fast: 0ms');
    expect(block).toContain('--motion-normal: 0ms');
    expect(tokensCss).toContain('@media (prefers-reduced-motion: reduce)');
  });

  it('disables the only animation (skeleton) in both mechanisms', () => {
    expect(motionCss).toContain("[data-reduce-motion='true'] .skeleton { animation: none !important; }");
    const reduceBlock = motionCss.slice(motionCss.lastIndexOf('@media (prefers-reduced-motion: reduce)'));
    expect(reduceBlock).toContain('.skeleton { animation: none; }');
  });

  it('declares no other infinite animation', () => {
    const infinite = [...motionCss.matchAll(/animation:[^;]*infinite[^;]*;/g)].map((m) => m[0]);
    expect(infinite).toHaveLength(1);
    expect(infinite[0]).toContain('skeleton-pulse');
    // transition/animation durations must come from tokens, never hard-coded
    const hardCoded = [...motionCss.matchAll(/transition:\s*[^;]*\d+ms/g)].map((m) => m[0]);
    expect(hardCoded).toHaveLength(0);
  });

  it('reflects the preference on the document root for CSS to act on', () => {
    applyPreferences({ ...DEFAULT_PREFERENCES, reduce_motion: true });
    expect(document.documentElement.dataset.reduceMotion).toBe('true');
    applyPreferences({ ...DEFAULT_PREFERENCES, reduce_motion: false });
    expect(document.documentElement.dataset.reduceMotion).toBe(String(prefersReducedMotion()));
  });

  it('exposes the OS preference through a single helper', () => {
    expect(typeof prefersReducedMotion()).toBe('boolean');
  });
});
