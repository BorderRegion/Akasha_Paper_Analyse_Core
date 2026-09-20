/**
 * Session + capabilities provider (spec docs/06 §会话与能力).
 *
 * The CSRF token lives ONLY in memory: it is never persisted to localStorage or
 * sessionStorage. Logging out clears the query cache so a revoked session cannot
 * leave private data on screen (docs/06: "撤销会话，清理客户端私密缓存").
 */

import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ApiClient } from '../api/client';
import type { Bootstrap, Capabilities, Session } from '../api/contract';
import { createUiApi, type UiApi } from '../api/ui';
import { ApiError } from '../api/errors';
import { clearSchemaIssues } from '../api/validate';

export interface SessionState {
  status: 'loading' | 'anonymous' | 'authenticated' | 'error';
  bootstrap: Bootstrap | null;
  capabilities: Capabilities | null;
  errorCode: string | null;
  login(token: string): Promise<void>;
  logout(): Promise<void>;
  refreshCapabilities(): Promise<void>;
}

interface SessionContextValue extends SessionState {
  api: UiApi;
  client: ApiClient;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export interface SessionProviderProps {
  client: ApiClient;
  children: ReactNode;
}

export function SessionProvider({ client, children }: SessionProviderProps): ReactElement {
  const queryClient = useQueryClient();
  const csrfRef = useRef<string | null>(null);
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [status, setStatus] = useState<SessionState['status']>('loading');
  const [errorCode, setErrorCode] = useState<string | null>(null);

  const api = useMemo(
    () => createUiApi(client, { getCsrfToken: () => csrfRef.current }),
    [client],
  );

  const loadCapabilities = useCallback(async () => {
    const response = await api.capabilities();
    setCapabilities(response.data);
    return response.data;
  }, [api]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const response = await api.bootstrap();
        if (cancelled) return;
        setBootstrap(response.data);
        if (!response.data.auth_required) {
          // Local deployment without auth: the surface is readable, and the
          // mode is REPORTED rather than pretended (docs/06).
          await loadCapabilities();
          if (!cancelled) setStatus('authenticated');
          return;
        }
        try {
          const current = await client.request<Session>('/v1/ui/session');
          if (cancelled) return;
          csrfRef.current = current.data.csrf_token;
          await loadCapabilities();
          if (!cancelled) setStatus('authenticated');
        } catch (error) {
          if (error instanceof ApiError && error.shape.http_status === 401) {
            if (!cancelled) setStatus('anonymous');
          } else {
            throw error;
          }
        }
      } catch (error) {
        if (cancelled) return;
        setStatus('error');
        setErrorCode(error instanceof ApiError ? error.shape.code : 'INTERNAL_001');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [api, client, loadCapabilities]);

  const login = useCallback(
    async (token: string) => {
      const response = await api.createSession(token);
      const session: Session = response.data;
      csrfRef.current = session.csrf_token;
      await loadCapabilities();
      setStatus('authenticated');
      setErrorCode(null);
    },
    [api, loadCapabilities],
  );

  const logout = useCallback(async () => {
    try {
      await api.destroySession(csrfRef.current);
    } finally {
      csrfRef.current = null;
      setCapabilities(null);
      clearSchemaIssues();
      // Private data must not survive a logout: drop every cached payload.
      queryClient.clear();
      setStatus(bootstrap?.auth_required ? 'anonymous' : 'authenticated');
    }
  }, [api, bootstrap?.auth_required, queryClient]);

  const value = useMemo<SessionContextValue>(
    () => ({
      status,
      bootstrap,
      capabilities,
      errorCode,
      login,
      logout,
      refreshCapabilities: async () => {
        await loadCapabilities();
      },
      api,
      client,
    }),
    [api, bootstrap, capabilities, client, errorCode, loadCapabilities, login, logout, status],
  );

  return createElement(SessionContext.Provider, { value }, children);
}

export function useSession(): SessionContextValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error('useSession must be used inside SessionProvider');
  return value;
}

/** Capability availability from the SERVER, never hard-coded in the UI. */
export function useCapability(code: string): { available: boolean; reason: string | null } {
  const { capabilities } = useSession();
  const entry = capabilities?.capabilities.find((item) => item.code === code);
  if (!entry) return { available: false, reason: 'capability not reported by the server' };
  return { available: entry.availability === 'AVAILABLE', reason: entry.reason };
}
