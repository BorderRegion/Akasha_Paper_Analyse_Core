/**
 * SafeText — the ONLY way untrusted text is rendered.
 *
 * HTML is escaped, Markdown is parsed into typed segments, links are limited to
 * http/https with noopener, and script-like content is displayed as literal text.
 */

import type { ReactElement } from 'react';
import { containsExecutableContent, parseSafeMarkdown } from '../../lib/safeText';

export interface SafeTextProps {
  value: string;
  /** Render inline Markdown (bold/italic/code/links). */
  markdown?: boolean;
  as?: 'p' | 'span' | 'div';
}

export function SafeText({ value, markdown = false, as = 'p' }: SafeTextProps): ReactElement {
  const segments = markdown ? parseSafeMarkdown(value) : [{ kind: 'text' as const, value }];
  const content = segments.map((segment, index) => {
    switch (segment.kind) {
      case 'strong':
        return <strong key={index}>{segment.value}</strong>;
      case 'em':
        return <em key={index}>{segment.value}</em>;
      case 'code':
        return <code key={index}>{segment.value}</code>;
      case 'link':
        // `noopener noreferrer` and no target=_blank injection by model output.
        return (
          <a key={index} href={segment.href} rel="noopener noreferrer" data-external="true">
            {segment.value}
          </a>
        );
      default:
        return <span key={index}>{segment.value}</span>;
    }
  });

  const element = as === 'div' ? 'div' : as === 'span' ? 'span' : 'p';
  return (
    <span
      data-executable-content={containsExecutableContent(value) ? 'literal' : undefined}
      data-testid="safe-text"
    >
      {element === 'div' ? <div>{content}</div> : element === 'span' ? <span>{content}</span> : <p>{content}</p>}
    </span>
  );
}
