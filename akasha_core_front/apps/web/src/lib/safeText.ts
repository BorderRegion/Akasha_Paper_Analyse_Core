/**
 * Safe rendering of untrusted text (docs/09 §权限和输入边界).
 *
 * PDF text, titles, notes and MODEL OUTPUT are all untrusted input:
 * - HTML is escaped by default; raw HTML is never injected;
 * - Markdown is rendered from a whitelist of inline constructs — no HTML
 *   passthrough, no images with remote sources, no scripts;
 * - links are limited to http/https and always get rel="noopener noreferrer";
 *   javascript:, data: and vbscript: URLs are dropped;
 * - nothing here executes model instructions: a string that looks like a
 *   directive stays a string.
 */

export interface SafeSegment {
  kind: 'text' | 'strong' | 'em' | 'code' | 'link';
  value: string;
  href?: string;
}

const ALLOWED_SCHEMES = ['http:', 'https:'];

/** True when a URL may be rendered as a link. */
export function isSafeHref(href: string): boolean {
  const trimmed = href.trim();
  if (/^\s*(javascript|data|vbscript|file):/i.test(trimmed)) return false;
  try {
    const url = new URL(trimmed, 'https://local.invalid');
    return ALLOWED_SCHEMES.includes(url.protocol);
  } catch {
    return false;
  }
}

/** Escape a string so it can never become markup. */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Parse a small Markdown subset into segments. Any HTML in the source is
 * returned as literal text (escaped later by React), and unsafe links lose
 * their href.
 */
export function parseSafeMarkdown(source: string): SafeSegment[] {
  const segments: SafeSegment[] = [];
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let cursor = 0;
  for (const match of source.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) segments.push({ kind: 'text', value: source.slice(cursor, index) });
    const token = match[0];
    if (token.startsWith('**')) {
      segments.push({ kind: 'strong', value: token.slice(2, -2) });
    } else if (token.startsWith('`')) {
      segments.push({ kind: 'code', value: token.slice(1, -1) });
    } else if (token.startsWith('*')) {
      segments.push({ kind: 'em', value: token.slice(1, -1) });
    } else {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      const label = linkMatch?.[1] ?? token;
      const rawHref = linkMatch?.[2] ?? '';
      if (isSafeHref(rawHref)) {
        segments.push({ kind: 'link', value: label, href: rawHref.trim() });
      } else {
        // The text stays, the dangerous href is dropped.
        segments.push({ kind: 'text', value: label });
      }
    }
    cursor = index + token.length;
  }
  if (cursor < source.length) segments.push({ kind: 'text', value: source.slice(cursor) });
  return segments.filter((segment) => segment.value.length > 0);
}

/** Script-ish content that must never be executed, only displayed. */
export function containsExecutableContent(source: string): boolean {
  return /<\s*script|onerror\s*=|onload\s*=|javascript:/i.test(source);
}
