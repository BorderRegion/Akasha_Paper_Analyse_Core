import type { ReactElement } from 'react';
import { HomePage } from '../features/home/HomePage';
import { LibraryPage } from '../features/library/LibraryPage';
import { PaperWorkspacePage } from '../features/paper/PaperWorkspacePage';
import { ReviewPage } from '../features/review/ReviewPage';
import { CollectionsPage } from '../features/collections/CollectionsPage';
import { CollectionWorkspacePage } from '../features/collections/CollectionWorkspacePage';
import { TechniquesPage } from '../features/techniques/TechniquesPage';
import { ComparePage } from '../features/compare/ComparePage';
import { OperationsPage } from '../features/operations/OperationsPage';
import { SettingsPage } from '../features/settings/SettingsPage';

export const ROUTES = {
  home: '/app/home',
  library: '/app/library',
  paper: '/app/papers/:paperId',
  review: '/app/review',
  collections: '/app/collections',
  collection: '/app/collections/:collectionId',
  techniques: '/app/techniques',
  compare: '/app/compare',
  operations: '/app/operations',
  settings: '/app/settings',
} as const;

export const routeElements: Record<string, ReactElement> = {
  home: <HomePage />,
  library: <LibraryPage />,
  paper: <PaperWorkspacePage />,
  review: <ReviewPage />,
  collections: <CollectionsPage />,
  collection: <CollectionWorkspacePage />,
  techniques: <TechniquesPage />,
  compare: <ComparePage />,
  operations: <OperationsPage />,
  settings: <SettingsPage />,
};
