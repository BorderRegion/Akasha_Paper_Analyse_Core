/**
 * Theme + motion preferences (spec docs/02 §全局结构, docs/05 §状态归属).
 *
 * Only non-sensitive UI preferences live in localStorage, scoped per origin +
 * workspace id so two deployments cannot read each other's draft state.
 */

export type ThemeChoice = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';

export interface Preferences {
  theme: ThemeChoice;
  density: 'comfortable' | 'compact';
  reduce_motion: boolean;
  single_key_shortcuts: boolean;
  reader_font_px: number;
  focus_default: boolean;
}

export const DEFAULT_PREFERENCES: Preferences = {
  theme: 'system',
  density: 'comfortable',
  reduce_motion: false,
  single_key_shortcuts: true,
  reader_font_px: 17,
  focus_default: false,
};

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || !window.matchMedia) return false;
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function systemTheme(): ResolvedTheme {
  if (typeof window === 'undefined' || !window.matchMedia) return 'light';
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

export function resolveTheme(choice: ThemeChoice): ResolvedTheme {
  return choice === 'system' ? systemTheme() : choice;
}

export function storageKey(workspaceId: string): string {
  return `paperintel.preferences.${workspaceId}`;
}

export function loadPreferences(workspaceId: string): Preferences {
  if (typeof window === 'undefined') return DEFAULT_PREFERENCES;
  try {
    const raw = window.localStorage.getItem(storageKey(workspaceId));
    if (!raw) return DEFAULT_PREFERENCES;
    const parsed = JSON.parse(raw) as Partial<Preferences>;
    const theme = String(parsed?.theme ?? '').toLowerCase();
    const density = String(parsed?.density ?? '').toLowerCase();
    return {
      theme: ['light', 'dark', 'system'].includes(theme) ? theme as ThemeChoice : DEFAULT_PREFERENCES.theme,
      density: density === 'compact' ? 'compact' : 'comfortable',
      reduce_motion: typeof parsed?.reduce_motion === 'boolean' ? parsed.reduce_motion : DEFAULT_PREFERENCES.reduce_motion,
      single_key_shortcuts: typeof parsed?.single_key_shortcuts === 'boolean' ? parsed.single_key_shortcuts : DEFAULT_PREFERENCES.single_key_shortcuts,
      focus_default: typeof parsed?.focus_default === 'boolean' ? parsed.focus_default : DEFAULT_PREFERENCES.focus_default,
      reader_font_px: typeof parsed?.reader_font_px === 'number' && Number.isFinite(parsed.reader_font_px)
        ? Math.min(32, Math.max(12, parsed.reader_font_px)) : DEFAULT_PREFERENCES.reader_font_px,
    };
  } catch {
    // Corrupted local preferences must not break the app; defaults are honest.
    return DEFAULT_PREFERENCES;
  }
}

export function savePreferences(workspaceId: string, prefs: Preferences): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(storageKey(workspaceId), JSON.stringify(prefs));
  } catch {
    // Memory preferences remain usable when browser storage is unavailable.
  }
}

/** Apply theme + motion to the document root; the CSS does the rest. */
export function applyPreferences(prefs: Preferences): ResolvedTheme {
  const theme = resolveTheme(prefs.theme);
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.density = prefs.density;
    document.documentElement.dataset.reduceMotion = String(
      prefs.reduce_motion || prefersReducedMotion(),
    );
    document.documentElement.style.setProperty('--reader-font-px', `${prefs.reader_font_px}px`);
  }
  return theme;
}
