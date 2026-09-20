/**
 * Preferences context (docs/06 §运维、设置与诊断 + docs/03 §S11).
 *
 * The stored preferences are read once per app instance and applied to the
 * document; the settings page edits the SAME value the shell renders, so a theme
 * change is visible immediately and survives a reload. A preferences API is
 * available separately; appearance choices here remain local to this browser.
 */

import {
  createContext,
  createElement,
  useContext,
  useEffect,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { applyPreferences, savePreferences, type Preferences } from './theme';

interface PreferencesContextValue {
  preferences: Preferences;
  update(patch: Partial<Preferences>): void;
}

const PreferencesContext = createContext<PreferencesContextValue | null>(null);

export function PreferencesProvider({
  workspaceId = 'local',
  initial,
  children,
}: {
  workspaceId?: string;
  initial: Preferences;
  children: ReactNode;
}): ReactElement {
  const [preferences, setPreferences] = useState<Preferences>(initial);
  useEffect(() => {
    const apply = () => applyPreferences(preferences);
    apply();
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
    media.addEventListener('change', apply);
    motion.addEventListener('change', apply);
    return () => {
      media.removeEventListener('change', apply);
      motion.removeEventListener('change', apply);
    };
  }, [preferences]);
  const update = (patch: Partial<Preferences>) => {
    setPreferences((current) => {
      const next = { ...current, ...patch };
      savePreferences(workspaceId, next);
      applyPreferences(next);
      return next;
    });
  };
  return createElement(
    PreferencesContext.Provider,
    { value: { preferences, update } },
    children,
  );
}

export function usePreferences(): PreferencesContextValue {
  const value = useContext(PreferencesContext);
  if (!value) throw new Error('usePreferences must be used inside PreferencesProvider');
  return value;
}
