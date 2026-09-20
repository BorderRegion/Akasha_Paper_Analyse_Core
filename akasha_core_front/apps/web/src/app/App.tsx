import { useState, type ReactElement } from 'react';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom';
import { Shell } from './shell/Shell';
import { ROUTES, routeElements } from './routes';
import { loadPreferences, type Preferences } from '../state/theme';
import { Providers } from './providers';
import { PreferencesProvider, usePreferences } from '../state/preferences';
import { ImportDialogProvider } from '../features/import/importContext';
import type { PdfEngineFactory } from '../reader/engineContext';
import { ApiClient, type Mode } from '../api/client';
import { recordEvent, routeTemplate } from '../state/clientEvents';
import { SessionGate } from '../features/auth/SessionGate';

export interface AppProps {
  /** Workspace id scopes local drafts/preferences/selection (docs/05 §并发编辑). */
  workspaceId?: string;
  /** Test/preview seam; production reads localStorage. */
  initialPreferences?: Preferences;
  /** LIVE (default) or TEST; failures are errors in both, never demo data. */
  mode?: Mode;
  /** Test seam: inject a fetch implementation (MSW, fixtures). */
  fetchImpl?: typeof fetch;
  /** Test seam: a fully configured client. */
  client?: ApiClient;
  /** Test seam: a deterministic PDF engine instead of bundled PDF.js. */
  pdfEngineFactory?: PdfEngineFactory;
}

function NotFound(): ReactElement {
  return (
    <section>
      <h1>页面不存在</h1>
      <p>该地址没有对应页面，未加载任何私有数据。</p>
    </section>
  );
}

function ConfiguredShell(): ReactElement {
  const { preferences } = usePreferences();
  return <Shell singleKeyShortcuts={preferences.single_key_shortcuts} defaultFocus={preferences.focus_default} />;
}

export function App({
  workspaceId = 'local',
  initialPreferences,
  mode = 'LIVE',
  fetchImpl,
  client,
  pdfEngineFactory,
}: AppProps): ReactElement {
  const [preferences] = useState<Preferences>(
    () => initialPreferences ?? loadPreferences(workspaceId),
  );
  const [apiClient] = useState<ApiClient>(
    () =>
      client ??
      new ApiClient({
        mode,
        fetchImpl,
        // Diagnostics: method + route TEMPLATE + status + duration + trace id.
        onEvent: ({ method, path, status, durationMs, traceId }) =>
          recordEvent({
            kind: 'request',
            detail: `${method} ${path}`,
            method,
            routeTemplate: routeTemplate(path),
            status,
            durationMs,
            traceId,
          }),
      }),
  );

  const [router] = useState(() => createBrowserRouter([
    { path: '/', element: <Navigate to={ROUTES.home} replace /> },
    {
      path: '/app',
      element: <ConfiguredShell />,
      children: [
        { index: true, element: <Navigate to={ROUTES.home} replace /> },
        { path: 'home', element: routeElements.home },
        { path: 'library', element: routeElements.library },
        { path: 'papers/:paperId', element: routeElements.paper },
        { path: 'review', element: routeElements.review },
        { path: 'collections', element: routeElements.collections },
        { path: 'collections/:collectionId', element: routeElements.collection },
        { path: 'techniques', element: routeElements.techniques },
        { path: 'compare', element: routeElements.compare },
        { path: 'operations', element: routeElements.operations },
        { path: 'settings', element: routeElements.settings },
      ],
    },
    { path: '*', element: <NotFound /> },
  ]));

  return (
    <Providers client={apiClient} pdfEngineFactory={pdfEngineFactory}>
      <PreferencesProvider workspaceId={workspaceId} initial={preferences}>
        <SessionGate>
          <ImportDialogProvider>
            <RouterProvider router={router} />
          </ImportDialogProvider>
        </SessionGate>
      </PreferencesProvider>
    </Providers>
  );
}
