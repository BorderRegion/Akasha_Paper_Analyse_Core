/** Pure formatting helpers — no component may invent a number or a unit. */

export interface MissingValue {
  value: null;
  missing_reason: string | null;
}

export const MISSING_LABELS: Record<string, string> = {
  NOT_REPORTED: '未报告',
  NOT_EXTRACTED: '未提取',
  NOT_MEASURED: '未测量',
  NOT_APPLICABLE: '不适用',
  UNAVAILABLE: '暂不可用',
  UNKNOWN: '未知',
};

/** null is never rendered as 0 (spec docs/05 §缺失值). */
export function formatNumber(value: number | null | undefined, missingReason?: string | null): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return missingLabel(missingReason);
  }
  return new Intl.NumberFormat('zh-CN').format(value);
}

export function formatBytes(value: number | null | undefined, missingReason?: string | null): string {
  if (value === null || value === undefined) return missingLabel(missingReason);
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let scaled = value;
  let unit = 0;
  while (scaled >= 1024 && unit < units.length - 1) {
    scaled /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${units[unit]}`;
}

export function formatPercent(value: number | null | undefined, missingReason?: string | null): string {
  if (value === null || value === undefined) return missingLabel(missingReason);
  return `${value.toFixed(1)}%`;
}

export function formatDateTime(value: string | null | undefined, missingReason?: string | null): string {
  if (!value) return missingLabel(missingReason);
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return missingLabel('UNKNOWN');
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed);
}

export function missingLabel(reason?: string | null): string {
  if (!reason) return '未知';
  return MISSING_LABELS[reason] ?? '未知';
}

/** Duration with an explicit unknown state — never a fabricated countdown. */
export function formatEtaRange(p50: number | null, p90: number | null, reason?: string | null): string {
  if (p50 === null || p90 === null) return missingLabel(reason ?? 'NOT_MEASURED');
  const fmt = (seconds: number) => {
    if (seconds < 90) return `${Math.round(seconds)} 秒`;
    if (seconds < 5400) return `${Math.round(seconds / 60)} 分钟`;
    return `${(seconds / 3600).toFixed(1)} 小时`;
  };
  return `${fmt(p50)} – ${fmt(p90)}`;
}
