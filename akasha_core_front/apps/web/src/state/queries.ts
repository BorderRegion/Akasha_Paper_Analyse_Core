/**
 * Server-state hooks (TanStack Query) over the typed `/v1/ui` API.
 *
 * Rules encoded here (docs/05 §Mutation协议, docs/06 §通用响应):
 * - reads are keyed by the FULL query, so a filter change is a different cache
 *   entry — no accidental reuse of another scope's page;
 * - a RESET_CURSOR response is not an error state: the filters are kept and the
 *   listing restarts from the first page with a visible notice;
 * - only reversible actions (favourite/save) update optimistically and roll back
 *   on failure; scientific state, tier, rerun and deletion always wait for the
 *   server receipt.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import type { LibraryQuery, PersonalState } from '../api/contract';
import { ApiError } from '../api/errors';
import { errorCodeOf } from '../lib/apiError';
import type { LibraryPagePayload, PersonalPatch } from '../api/ui';
import { useSession } from './session';

export interface LibraryResult {
  page: LibraryPagePayload;
  /** Set when the server refused a cursor: the client restarted from page 1. */
  resetNotice: string | null;
}

export function libraryQueryKey(query: LibraryQuery) {
  return ['ui', 'library', query] as const;
}

export function useLibrary(query: LibraryQuery): UseQueryResult<LibraryResult, ApiError> {
  const { api } = useSession();
  return useQuery<LibraryResult, ApiError>({
    queryKey: libraryQueryKey(query),
    queryFn: async () => {
      try {
        const response = await api.libraryQuery(query);
        return { page: response.data, resetNotice: null };
      } catch (error) {
        if (error instanceof ApiError && error.shape.code === 'RESET_CURSOR') {
          // Keep the filters, drop the cursor, and SAY so instead of silently
          // showing page 1 as if it were the requested page.
          const restarted = await api.libraryQuery({ ...query, cursor: null });
          return {
            page: restarted.data,
            resetNotice: '结果范围已变化，已从第一页重新载入（筛选条件保留）。',
          };
        }
        throw error;
      }
    },
    staleTime: 15_000,
  });
}

export function useWorkspace(paperId: string, paperVersionId?: string | null) {
  const { api } = useSession();
  return useQuery({
    queryKey: ['ui', 'workspace', paperId, paperVersionId ?? null] as const,
    queryFn: async () => (await api.workspace(paperId, paperVersionId ?? null)).data,
    enabled: Boolean(paperId),
  });
}

export interface SavedMutationVariables {
  paperId: string;
  saved: boolean;
  /** The revision the user saw; the server rejects a stale write. */
  expectedRevision?: number;
  query: LibraryQuery;
}

/**
 * Favourite toggle: optimistic for immediate feedback, rolled back on failure
 * with a message that keeps the previous state (docs/05).
 */
export function useSavedMutation(): UseMutationResult<
  PersonalState,
  ApiError,
  SavedMutationVariables
> {
  const { api } = useSession();
  const queryClient = useQueryClient();

  return useMutation<PersonalState, ApiError, SavedMutationVariables>({
    mutationFn: async ({ paperId, saved, expectedRevision }) =>
      (
        await api.patchPersonal(paperId, {
          saved,
          ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
        })
      ).data,
    onMutate: async ({ paperId, saved, query }) => {
      const key = libraryQueryKey(query);
      await queryClient.cancelQueries({ queryKey: key });
      const previous = queryClient.getQueryData<LibraryResult>(key);
      if (previous?.page.kind === 'PAPERS') {
        queryClient.setQueryData<LibraryResult>(key, {
          ...previous,
          page: {
            ...previous.page,
            items: previous.page.items.map((item) =>
              item.paper_id === paperId
                ? { ...item, personal: { ...item.personal, saved } }
                : item,
            ),
          },
        });
      }
      return { previous, key };
    },
    onError: (_error, _variables, context) => {
      const typed = context as { previous?: LibraryResult; key?: readonly unknown[] } | undefined;
      if (typed?.previous && typed.key) queryClient.setQueryData(typed.key, typed.previous);
    },
    onSuccess: (_state, { paperId }) => {
      queryClient.invalidateQueries({ queryKey: ['ui', 'workspace', paperId] });
    },
    onSettled: (_data, _error, variables) => {
      queryClient.invalidateQueries({ queryKey: libraryQueryKey(variables.query) });
    },
  });
}

export function usePersonalMutation(paperId: string) {
  const { api } = useSession();
  const queryClient = useQueryClient();
  return useMutation<PersonalState, ApiError, PersonalPatch>({
    mutationFn: async (patch) => (await api.patchPersonal(paperId, patch)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ui', 'workspace', paperId] });
      queryClient.invalidateQueries({ queryKey: ['ui', 'library'] });
    },
  });
}

/**
 * Import batch polling. It polls only while something can still change, so a
 * finished batch stops costing requests (docs/03 §S03).
 */
export function useImportBatch(batchId: string | null) {
  const { api } = useSession();
  return useQuery({
    queryKey: ['ui', 'import-batch', batchId] as const,
    enabled: Boolean(batchId),
    queryFn: async () => (await api.importBatch(batchId as string)).data,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 2000;
      const settled = data.items.every((item) =>
        ['IMPORTED', 'DUPLICATE', 'FAILED', 'CANCELLED'].includes(item.state),
      );
      return settled ? false : 2000;
    },
  });
}

export interface UploadRow {
  /** Stable client-side key: the idempotency key the server receives. */
  key: string;
  file: File;
  itemId: string | null;
  state:
    | 'PENDING'
    | 'UPLOADING'
    | 'RECEIVED'
    | 'QUEUED'
    | 'IMPORTED'
    | 'DUPLICATE'
    | 'FAILED'
    | 'CANCELLED';
  errorCode: string | null;
  paperId: string | null;
  jobId: string | null;
  retryable?: boolean;
}

export interface UploadOutcome {
  item_id: string;
  state: string;
  error_code: string | null;
  paper_id: string | null;
  job_id: string | null;
}

export interface UploadQueueOptions {
  batchId: string;
  /** Performs the actual multipart request (injected so the queue is testable). */
  upload(
    batchId: string,
    file: File,
    options: { idempotencyKey: string },
  ): Promise<UploadOutcome>;
  onChange(rows: UploadRow[]): void;
  /** Client-side limit mirrored from capabilities. */
  concurrency?: number;
  maxFileBytes?: number;
  maxFiles?: number;
  maxBatchBytes?: number;
}

/**
 * The client-side upload queue.
 *
 * - at most `concurrency` (2 by default) files are in flight (docs/06 §导入);
 * - each file gets ONE idempotency key that is reused on retry, so a retry can
 *   never produce a second paper;
 * - one failed file never touches the other rows (docs/03 §S03);
 * - limits come from capabilities, and an over-limit file is refused BEFORE any
 *   bytes are sent.
 */
export class UploadQueue {
  private readonly options: UploadQueueOptions;
  private readonly concurrency: number;
  private rows: UploadRow[] = [];
  private readonly pending: string[] = [];
  private running = 0;
  private sentBytes = 0;
  private live = true;

  constructor(options: UploadQueueOptions) {
    this.options = options;
    this.concurrency = Math.max(1, options.concurrency ?? 2);
  }

  get currentRows(): UploadRow[] {
    return this.rows.map((row) => ({ ...row }));
  }

  dispose(): void {
    this.live = false;
  }

  enqueueFiles(files: File[]): UploadRow[] {
    for (const file of files) {
      const key = `${this.options.batchId}:${file.name}:${file.size}:${file.lastModified}`;
      if (this.rows.some((row) => row.key === key)) continue;
      const overCount = this.options.maxFiles !== undefined && this.rows.length >= this.options.maxFiles;
      const overSize = this.options.maxFileBytes !== undefined && file.size > this.options.maxFileBytes;
      const overBatch =
        this.options.maxBatchBytes !== undefined && this.sentBytes + file.size > this.options.maxBatchBytes;
      if (overCount || overSize || overBatch) {
        // Refused locally with the SAME codes the server would use, so the
        // message is identical whichever side catches it first.
        this.rows.push({
          key,
          file,
          itemId: null,
          state: 'FAILED',
          errorCode: overCount ? 'CFG_002' : 'STORAGE_001',
          paperId: null,
          jobId: null,
          retryable: false,
        });
        continue;
      }
      this.sentBytes += file.size;
      this.rows.push({
        key,
        file,
        itemId: null,
        state: 'PENDING',
        errorCode: null,
        paperId: null,
        jobId: null,
      });
      this.pending.push(key);
    }
    this.publish();
    void this.drain();
    return this.currentRows;
  }

  /** Retry the named failed rows; the idempotency key is reused unchanged. */
  retryFailed(keys: string[]): UploadRow[] {
    for (const key of keys) {
      const row = this.rows.find((candidate) => candidate.key === key);
      if (!row || row.state !== 'FAILED' || row.retryable === false) continue;
      row.state = 'PENDING';
      row.errorCode = null;
      this.pending.push(key);
    }
    this.publish();
    void this.drain();
    return this.currentRows;
  }

  cancel(key: string): UploadRow[] {
    const row = this.rows.find((candidate) => candidate.key === key);
    if (row && (row.state === 'PENDING' || row.state === 'UPLOADING')) {
      row.state = 'CANCELLED';
      const index = this.pending.indexOf(key);
      if (index >= 0) this.pending.splice(index, 1);
    }
    this.publish();
    return this.currentRows;
  }

  private async drain(): Promise<void> {
    while (this.live && this.running < this.concurrency && this.pending.length) {
      const key = this.pending.shift() as string;
      const row = this.rows.find((candidate) => candidate.key === key);
      if (!row || row.state !== 'PENDING') continue;
      this.running += 1;
      void this.send(row).finally(() => {
        this.running -= 1;
        void this.drain();
      });
    }
  }

  private async send(row: UploadRow): Promise<void> {
    row.state = 'UPLOADING';
    this.publish();
    try {
      const outcome = await this.options.upload(this.options.batchId, row.file, {
        idempotencyKey: row.key,
      });
      if (!this.live) return;
      row.itemId = outcome.item_id;
      row.state = outcome.state as UploadRow['state'];
      row.errorCode = outcome.error_code;
      row.paperId = outcome.paper_id;
      row.jobId = outcome.job_id;
    } catch (error) {
      if (!this.live) return;
      row.state = 'FAILED';
      row.errorCode = errorCodeOf(error) ?? 'STORAGE_001';
    }
    this.publish();
  }

  private publish(): void {
    if (this.live) this.options.onChange(this.currentRows);
  }
}
