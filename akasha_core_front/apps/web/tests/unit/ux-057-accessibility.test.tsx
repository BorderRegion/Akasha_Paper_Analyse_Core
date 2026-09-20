/**
 * UX-057 — accessibility: automated serious/critical checks plus a recorded
 * manual walkthrough.
 *
 * Requirement (docs/08 §可访问性): CI检查axe serious/critical=0、键盘可达、dialog焦点、
 * reduce-motion和200%缩放。截图通过不等于研究信息正确。
 *
 * The automated part runs axe-core against the real pages in jsdom: it catches
 * missing names, broken landmarks, invalid ARIA and contrast-independent issues.
 * The MANUAL part (screen reader, keyboard-only, 200% zoom, 320px) is recorded in
 * `docs/a11y_walkthrough.md` — an automated pass is not evidence that a person
 * can use the page.
 */


import { describe, expect, it } from 'vitest';
import axe from 'axe-core';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

async function analyse(container: HTMLElement) {
  const results = await axe.run(container, {
    resultTypes: ['violations'],
    rules: {
      // jsdom has no layout: contrast and target-size are checked in the manual
      // walkthrough, which is recorded separately.
      'color-contrast': { enabled: false },
      'target-size': { enabled: false },
    },
  });
  return results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
}

const SERVER_ROUTES: Array<[string, Record<string, unknown>]> = [
  ['/app/home', { papers: [{ paper_id: 'pap_1', title: 'Home paper' }] }],
  ['/app/library', { papers: [{ paper_id: 'pap_1', title: 'Library paper' }] }],
  ['/app/operations', { snapshot: { queue: { pending: 0, running: 0, failed: 0 }, workers: [], modules: [], providers: [], disk: [], eta_available: false, observed_at: 'now', unknown_reasons: [] } }],
  ['/app/settings', {}],
  ['/app/review', { reviewItems: [] }],
  ['/app/techniques', { entities: [] }],
  ['/app/compare', {}],
];

describe('UX-057 accessibility checks', () => {
  for (const [route, options] of SERVER_ROUTES) {
    it(`has no serious/critical axe violations on ${route}`, async () => {
      const server = createFakeServer(options);
      const view = renderApp(server, { path: route });
      await new Promise((resolve) => setTimeout(resolve, 60));
      const violations = await analyse(view.container);
      expect(
        violations.map((violation) => `${violation.id} (${violation.nodes.length})`),
      ).toEqual([]);
    });
  }

  it('runs axe against a rendered control set', async () => {
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Axe paper' }] });
    const view = renderApp(server, { path: '/app/library' });
    await new Promise((resolve) => setTimeout(resolve, 60));
    const violations = await analyse(view.container);
    expect(violations).toHaveLength(0);
  });

});
