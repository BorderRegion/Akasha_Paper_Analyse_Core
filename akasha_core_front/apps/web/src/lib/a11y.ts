/**
 * Accessibility helpers (spec docs/08 §辅助功能).
 *
 * Contrast is computed here so the token sets can be asserted in tests rather
 * than eyeballed: WCAG 2.1 relative luminance and contrast ratio.
 */

export interface Rgb {
  r: number;
  g: number;
  b: number;
}

export function parseHexColor(value: string): Rgb {
  const hex = value.trim().replace('#', '');
  const full = hex.length === 3 ? hex.split('').map((c) => c + c).join('') : hex;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) {
    throw new Error(`not a hex color: ${value}`);
  }
  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  };
}

function channel(value: number): number {
  const c = value / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

export function relativeLuminance(color: Rgb): number {
  return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b);
}

export function contrastRatio(foreground: string, background: string): number {
  const a = relativeLuminance(parseHexColor(foreground));
  const b = relativeLuminance(parseHexColor(background));
  const [light, dark] = a > b ? [a, b] : [b, a];
  return (light + 0.05) / (dark + 0.05);
}

/** WCAG AA thresholds used by the F01 checks. */
export const CONTRAST_BODY_TEXT = 4.5;
export const CONTRAST_LARGE_TEXT_OR_UI = 3.0;

/** Minimum pointer target (WCAG 2.2 AA target size). */
export const MIN_TARGET_PX = 24;

export function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  // `isContentEditable` is not implemented everywhere (jsdom, some embedded
  // webviews), so the attribute is checked as well rather than trusting one
  // property that may be undefined.
  if (target.isContentEditable === true) return true;
  const attr = target.getAttribute('contenteditable');
  return attr === '' || attr === 'true' || attr === 'plaintext-only';
}

/** True while an IME composition is in flight (single-key shortcuts must wait). */
export function isComposing(event: KeyboardEvent): boolean {
  return event.isComposing || event.keyCode === 229;
}
