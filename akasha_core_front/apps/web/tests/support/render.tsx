/**
 * Shared test helpers for page-level tests.
 *
 * `renderApp` mounts the real App against a fake server and WAITS for the
 * one-time version pinning navigation to settle. Interacting before that
 * navigation finishes means clicking a node that React is about to replace —
 * the action is silently lost, which looks like a product bug but is a test
 * race.
 */

import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom';
import { Providers } from '../../src/app/providers';
import { PreferencesProvider } from '../../src/state/preferences';
import { loadPreferences } from '../../src/state/theme';
import { Shell } from '../../src/app/shell/Shell';
import { HomePage } from '../../src/features/home/HomePage';
import { ImportDialogProvider } from '../../src/features/import/importContext';
import { LibraryPage } from '../../src/features/library/LibraryPage';
import { PaperWorkspacePage } from '../../src/features/paper/PaperWorkspacePage';
import { ReviewPage } from '../../src/features/review/ReviewPage';
import { CollectionsPage } from '../../src/features/collections/CollectionsPage';
import { CollectionWorkspacePage } from '../../src/features/collections/CollectionWorkspacePage';
import { TechniquesPage } from '../../src/features/techniques/TechniquesPage';
import { ComparePage } from '../../src/features/compare/ComparePage';
import { OperationsPage } from '../../src/features/operations/OperationsPage';
import { SettingsPage } from '../../src/features/settings/SettingsPage';
import { ApiClient } from '../../src/api/client';
import { createFakeEngine } from './fake-engine';
import type { createFakeServer } from './fake-server';

export type FakeServer = ReturnType<typeof createFakeServer>;

export function renderApp(
  server: FakeServer,
  options: {
    path?: string;
    engineFactory?: () => Promise<ReturnType<typeof createFakeEngine>>;
    queryClient?: QueryClient;
  } = {},
) {
  if (options.path) window.history.pushState({}, '', options.path);
  const client = new ApiClient({ mode: 'TEST', fetchImpl: server.fetch });
  const result = render(
    <Providers
      client={client}
      queryClient={options.queryClient}
      pdfEngineFactory={options.engineFactory ?? (async () => createFakeEngine())}
    >
      <PreferencesProvider initial={loadPreferences('local')}>
        <ImportDialogProvider><RouterProvider router={createTestRouter()} /></ImportDialogProvider>
      </PreferencesProvider>
    </Providers>,
  );
  return result;
}

/** The same route table the real App uses, without a second App instance. */
function createTestRouter() {
  return createBrowserRouter([
    { path: '/', element: <Navigate to="/app/home" replace /> },
    {
      path: '/app',
      element: <Shell singleKeyShortcuts />,
      children: [
        { path: 'home', element: <HomePage /> },
        { path: 'library', element: <LibraryPage /> },
        { path: 'papers/:paperId', element: <PaperWorkspacePage /> },
        { path: 'review', element: <ReviewPage /> },
        { path: 'collections', element: <CollectionsPage /> },
        { path: 'collections/:collectionId', element: <CollectionWorkspacePage /> },
        { path: 'techniques', element: <TechniquesPage /> },
        { path: 'compare', element: <ComparePage /> },
        { path: 'operations', element: <OperationsPage /> },
        { path: 'settings', element: <SettingsPage /> },
      ],
    },
    { path: '*', element: <div>页面不存在</div> },
  ]);
}

/** Wait until the workspace is loaded and the pinned version is in the URL. */
export async function waitForWorkspace(): Promise<void> {
  await screen.findByTestId('pinned-version');
  await waitFor(() => expectPinnedVersion());
}

function expectPinnedVersion(): void {
  if (!window.location.search.includes('paper_version_id=')) {
    throw new Error('the pinned version is not in the URL yet');
  }
}
