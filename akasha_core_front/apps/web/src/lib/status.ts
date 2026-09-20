/**
 * Status vocabulary (design/status-map.json, spec docs/05).
 *
 * Rules encoded here:
 * - a server enum the UI does not know is shown as "未知状态（原值）" with the raw
 *   value preserved and a contract warning recorded — never a crash, never
 *   silently treated as FAILED;
 * - tone is presentation only: support is NOT a probability and never a score.
 */
import statusMap from '../../../../design/status-map.json';

export type Tone = 'muted' | 'info' | 'success' | 'warning' | 'danger';

export interface StatusEntry {
  label: string;
  tone: Tone;
  icon?: string;
}

export interface ResolvedStatus {
  label: string;
  tone: Tone;
  icon?: string;
  /** True when the value was not in the frozen vocabulary. */
  unknown: boolean;
  /** Raw server value, preserved for the unknown case (status-map.fallback). */
  raw: string;
}

export type StatusFamily = 'support' | 'task' | 'health';

const FAMILIES: Record<StatusFamily, Record<string, StatusEntry>> = {
  support: statusMap.support as Record<string, StatusEntry>,
  task: statusMap.task as Record<string, StatusEntry>,
  health: statusMap.health as Record<string, StatusEntry>,
};

export const CLAIM_TYPE_LABELS = statusMap.claim_type as Record<string, string>;
export const TIER_LABELS = statusMap.tiers as Record<string, string>;
export const READ_STATE_LABELS = statusMap.personal as Record<string, string>;

/** Unknown values seen this session (surfaced in DebugInspector, never swallowed). */
const unknownValues = new Set<string>();

export function resolveStatus(family: StatusFamily, value: string | null | undefined): ResolvedStatus {
  if (!value) {
    return { label: '未报告', tone: 'muted', unknown: false, raw: '' };
  }
  const entry = FAMILIES[family][value];
  if (entry) {
    return { ...entry, unknown: false, raw: value };
  }
  unknownValues.add(`${family}:${value}`);
  return {
    label: `${statusMap.fallback.label}（${value}）`,
    tone: statusMap.fallback.tone as Tone,
    unknown: true,
    raw: value,
  };
}

export function unknownStatusValues(): string[] {
  return [...unknownValues].sort();
}

export function clearUnknownStatusValues(): void {
  unknownValues.clear();
}

export function claimTypeLabel(value: string): string {
  return CLAIM_TYPE_LABELS[value] ?? `${statusMap.fallback.label}（${value}）`;
}

export function tierLabel(value: string | null): string | null {
  if (!value) return null;
  return TIER_LABELS[value] ?? `${statusMap.fallback.label}（${value}）`;
}

export function readStateLabel(value: string): string {
  return READ_STATE_LABELS[value] ?? `${statusMap.fallback.label}（${value}）`;
}
