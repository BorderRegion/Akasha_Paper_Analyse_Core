/**
 * The typed `/v1/ui` surface (spec docs/06 + contracts/endpoints.json).
 *
 * One function per documented endpoint, each returning the contract type from
 * `contract.ts`. Nothing here invents fields: a response the server did not
 * send stays `null` and is rendered as "not reported" by the components.
 */

import type { ApiClient, ClientResponse } from './client';
import type {
  Bootstrap,
  Capabilities,
  ImportBatch,
  LibraryQuery,
  Note,
  PaperListItem,
  PersonalState,
  Preferences,
  Session,
  Workspace,
} from './contract';

export interface PageData<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  total: number | null;
  total_kind: 'EXACT' | 'ESTIMATE' | 'UNKNOWN';
  scope_revision?: string | null;
  /** Result discriminator: a CLAIM hit is never a paper card. */
  kind?: string;
}

export interface PaperRowPayload {
  paper_id: string;
  paper_version_id: string;
  title: string;
  authors: PaperListItem['authors'];
  year: PaperListItem['year'];
  venue: PaperListItem['venue'];
  takeaway: PaperListItem['takeaway'];
  tags: PaperListItem['tags'];
  tier: PaperListItem['tier'];
  personal: PersonalState;
  audit: PaperListItem['audit'];
}

export interface ClaimHit {
  claim_id: string;
  statement: string;
  claim_type: string;
  support_state: string;
  paper_id: string;
  paper_version_id: string;
  evidence_count: number;
}

export interface TechniqueHit {
  entity_id: string;
  entity_type: string;
  name: string;
  aliases: string[];
}

export type LibraryPagePayload =
  | (PageData<PaperRowPayload> & { kind: 'PAPERS' })
  | (PageData<ClaimHit> & { kind: 'CLAIMS' })
  | (PageData<TechniqueHit> & { kind: 'TECHNIQUES' });

export interface PersonalPatch {
  saved?: boolean;
  read_state?: PersonalState['read_state'];
  reading_anchor?: {
    paper_version_id: string;
    page_number?: number | null;
    section_id?: string | null;
    scroll_offset?: number | null;
  } | null;
  expected_revision?: number;
}

export interface ImportItemResult {
  batch_id: string;
  item_id: string;
  state: string;
  sha256: string | null;
  size_bytes: number;
  created?: boolean;
  replayed?: boolean;
  paper_id: string | null;
  paper_version_id: string | null;
  job_id: string | null;
  error_code: string | null;
}

export interface OperationRequest {
  kind: string;
  payload?: Record<string, unknown>;
  idempotency_key?: string;
}

export interface ReviewGroup {
  group:
    | 'NUMERIC_OR_OCR'
    | 'EVIDENCE_UNSUPPORTED'
    | 'CONTRADICTION'
    | 'SCOPE'
    | 'NOVELTY_UNVERIFIED'
    | 'SUPPORT_STATE'
    | 'NOT_VERIFIED';
}

export interface ReviewQueueItem {
  claim: {
    claim_id: string;
    statement: string;
    claim_type: string;
    support_state: string;
    paper_version_id: string;
    evidence_count: number;
    is_superseded: boolean;
  };
  paper_id: string;
  paper_version_id: string;
  paper_title: string;
  group: ReviewGroup['group'];
  reasons: string[];
  impact: 'HIGH' | 'NORMAL' | 'LOW';
  source_refs: { paper_id: string; paper_version_id: string; claim_id: string | null; evidence_ids: string[] }[];
  original_evidence: { paper_id: string; paper_version_id: string; claim_id: string | null; evidence_ids: string[] }[];
  counter_evidence: { paper_id: string; paper_version_id: string; claim_id: string | null; evidence_ids: string[] }[];
  verifications: {
    verification_id: string;
    verifier_type: string;
    status: string;
    verdict: string;
    reason_summary: string;
    run_id: string | null;
  }[];
  claim_revision: string;
  personal_decision: 'SEEN' | 'NEEDS_REVIEW' | 'RESERVATION' | null;
}

export interface ReviewQueryInput {
  collection_id?: string | null;
  groups?: string[] | null;
  include_seen?: boolean;
  include_all?: boolean;
  paper_version_id?: string | null;
  limit?: number;
  offset?: number;
}

export interface HandoffManifest {
  asset_id: string;
  size_bytes: number;
  expires_at: string | null;
  redacted_fields: string[];
  manifest: Record<string, unknown>;
}

export interface SavedSearch {
  saved_search_id: string;
  name: string;
  query: LibraryQuery;
  revision: number;
}

export interface TagActionPreview {
  operation_id: string;
  action: string;
  state: string;
  affected_count: number;
  affected_paper_ids: string[];
  tags: string[];
  target_tag_id: string | null;
  conflict: { reason: string; namespaces?: string[]; codes?: string[] } | null;
  note: string;
}

export interface EntityCard {
  entity_id: string;
  entity_type: string;
  name: string;
  aliases: string[];
  description: { value: string | null; missing_reason: string | null; source_refs: unknown[] };
  relations?: { relation_type: string; direction: string; other_entity_id: string; confidence: number | null }[];
  source_refs: { paper_id: string; paper_version_id: string | null; claim_id: string | null; evidence_ids: string[] }[];
}

export interface ComparisonCell {
  paper_version_id: string;
  dimension: string;
  value: unknown;
  unit: string | null;
  protocol_key: string | null;
  comparable: boolean;
  comparability_reason: string | null;
  source_refs: { paper_id: string; paper_version_id: string; claim_id: string | null; evidence_ids: string[] }[];
  missing_reason: string | null;
}

export interface Comparison {
  paper_version_ids: string[];
  selection_hash: string;
  analysis_revision: string;
  cells: ComparisonCell[];
  papers: Record<string, { paper_id: string; version_label: string; document_sha256: string; publication_date: string | null }>;
}

export interface CollectionWorkspace {
  collection_id: string;
  title: string;
  selection_hash: string;
  selected_paper_count: number;
  analyzed_paper_count: number;
  analysis_revision: string;
  sample: { paper_versions: number; claims: number; years: number[] | null };
  insights: { title: string; claim_type: string; statement: string; source_refs: unknown[] }[];
}

export interface OperationResult {
  operation_id: string;
  kind: string;
  state: string;
  scope: Record<string, unknown>;
  job_ids: string[];
  results: { target_id: string; status: string; error_code: string | null }[];
  error_code: string | null;
}

export interface UiApi {
  createCollection(name: string): Promise<ClientResponse<{ collection_id: string; name: string }>>;
  collectionPaper(collectionId: string, paperId: string, remove?: boolean): Promise<ClientResponse<{ changed?: boolean; removed?: boolean }>>;
  diagnostics(input: Record<string, unknown>): Promise<ClientResponse<{
    asset_id: string; size_bytes: number; max_bytes?: number;
  }>>;
  bootstrap(): Promise<ClientResponse<Bootstrap>>;
  createSession(token: string): Promise<ClientResponse<Session>>;
  destroySession(csrfToken: string | null): Promise<ClientResponse<Session>>;
  capabilities(): Promise<ClientResponse<Capabilities>>;
  preferences(): Promise<ClientResponse<Preferences & { revision: number }>>;
  patchPreferences(
    patch: Partial<Preferences>,
    options?: { expectedRevision?: number },
  ): Promise<ClientResponse<Preferences & { revision: number }>>;

  libraryQuery(query: LibraryQuery): Promise<ClientResponse<LibraryPagePayload>>;
  workspace(paperId: string, paperVersionId?: string | null): Promise<ClientResponse<Workspace>>;
  paperJobs(paperId: string, options?: { paperVersionId?: string }): Promise<
    ClientResponse<PageData<Record<string, unknown>>>
  >;

  patchPersonal(paperId: string, patch: PersonalPatch): Promise<ClientResponse<PersonalState>>;
  notes(paperId: string): Promise<ClientResponse<{ items: Note[] }>>;
  createNote(input: {
    paper_id: string;
    paper_version_id: string;
    body: string;
    claim_id?: string | null;
    evidence_id?: string | null;
    paper_wide?: boolean;
  }): Promise<ClientResponse<Note>>;
  patchNote(
    noteId: string,
    input: { body?: string; expected_revision?: number },
  ): Promise<ClientResponse<Note>>;

  createImportBatch(input?: {
    collection_ids?: string[];
    requested_tier?: string;
  }): Promise<ClientResponse<ImportBatch>>;
  importBatch(batchId: string): Promise<ClientResponse<ImportBatch>>;
  retryImportItems(batchId: string, itemIds: string[]): Promise<ClientResponse<{ retried: string[] }>>;
  cancelImportItem(batchId: string, itemId: string): Promise<ClientResponse<{ item_id: string }>>;
  uploadFile(
    batchId: string,
    file: File,
    options?: { idempotencyKey?: string; signal?: AbortSignal },
  ): Promise<ClientResponse<ImportItemResult>>;

  submitOperation(request: OperationRequest): Promise<ClientResponse<OperationResult>>;

  reviewQuery(input: ReviewQueryInput): Promise<
    ClientResponse<PageData<ReviewQueueItem> & { kind: string }>
  >;
  reviewDecision(input: {
    claim_id: string;
    decision: 'SEEN' | 'NEEDS_REVIEW' | 'RESERVATION';
    note?: string | null;
    expected_claim_revision?: string;
    idempotency_key?: string;
  }): Promise<ClientResponse<{ decision_id: string; claim_id: string; decision: string; created: boolean }>>;
  createHandoff(input: {
    paper_version_ids: string[];
    include: string[];
    limit?: number;
    max_bytes?: number;
  }): Promise<ClientResponse<HandoffManifest>>;

  collectionWorkspace(collectionId: string): Promise<ClientResponse<CollectionWorkspace>>;
  compare(paperVersionIds: string[]): Promise<ClientResponse<Comparison>>;
  entitiesQuery(input: { query?: string; entity_type?: string | null; limit?: number }): Promise<
    ClientResponse<PageData<EntityCard> & { kind: string }>
  >;
  entity(entityId: string): Promise<ClientResponse<EntityCard>>;
  savedSearches(): Promise<ClientResponse<{ items: SavedSearch[] }>>;
  createSavedSearch(input: { name: string; query: LibraryQuery }): Promise<ClientResponse<SavedSearch>>;
  patchSavedSearch(
    id: string,
    input: { name?: string; query?: LibraryQuery; expected_revision?: number },
  ): Promise<ClientResponse<SavedSearch>>;
  deleteSavedSearch(id: string): Promise<ClientResponse<{ deleted: boolean; target_id: string }>>;
  tagAction(input: {
    action: 'confirm' | 'merge' | 'rename';
    tag_ids: string[];
    target_tag_id?: string | null;
    canonical_name?: string | null;
    preview_only?: boolean;
    idempotency_key?: string;
  }): Promise<ClientResponse<TagActionPreview>>;
}

export interface UiApiOptions {
  /** Session CSRF token for cookie-authenticated writes (docs/06). */
  getCsrfToken: () => string | null;
}

export function createUiApi(client: ApiClient, options: UiApiOptions): UiApi {
  const csrf = () => options.getCsrfToken();

  return {
    createCollection: (name) => client.request('/v1/ui/collections', { method: 'POST', body: { name }, csrfToken: csrf() }),
    collectionPaper: (collectionId, paperId, remove = false) => client.request(
      `/v1/ui/collections/${encodeURIComponent(collectionId)}/papers/${encodeURIComponent(paperId)}`,
      { method: remove ? 'DELETE' : 'POST', csrfToken: csrf() },
    ),
    bootstrap: () => client.request<Bootstrap>('/v1/ui/bootstrap'),

    createSession: (token) =>
      client.request<Session>('/v1/ui/session', { method: 'POST', body: { token } }),

    destroySession: (csrfToken) =>
      client.request<Session>('/v1/ui/session', { method: 'DELETE', csrfToken }),

    capabilities: () => client.request<Capabilities>('/v1/ui/capabilities'),

    preferences: () =>
      client.request<Preferences & { revision: number }>('/v1/ui/preferences'),

    patchPreferences: (patch, requestOptions) =>
      client.request<Preferences & { revision: number }>('/v1/ui/preferences', {
        method: 'PATCH',
        body: patch,
        csrfToken: csrf(),
        expectedRevision: requestOptions?.expectedRevision,
      }),

    libraryQuery: (query) =>
      client.request<LibraryPagePayload>('/v1/ui/library/query', {
        method: 'POST',
        body: query,
      }),

    workspace: (paperId, paperVersionId) =>
      client.request<Workspace>(
        `/v1/ui/papers/${encodeURIComponent(paperId)}/workspace${
          paperVersionId ? `?paper_version_id=${encodeURIComponent(paperVersionId)}` : ''
        }`,
      ),

    paperJobs: (paperId, requestOptions) =>
      client.request<PageData<Record<string, unknown>>>(
        `/v1/ui/papers/${encodeURIComponent(paperId)}/jobs${
          requestOptions?.paperVersionId
            ? `?paper_version_id=${encodeURIComponent(requestOptions.paperVersionId)}`
            : ''
        }`,
      ),

    patchPersonal: (paperId, patch) =>
      client.request<PersonalState>(`/v1/ui/papers/${encodeURIComponent(paperId)}/personal`, {
        method: 'PATCH',
        body: patch,
        csrfToken: csrf(),
      }),

    notes: (paperId) =>
      client.request<{ items: Note[] }>(`/v1/ui/papers/${encodeURIComponent(paperId)}/notes`),

    createNote: (input) =>
      client.request<Note>('/v1/ui/notes', { method: 'POST', body: input, csrfToken: csrf() }),

    patchNote: (noteId, input) =>
      client.request<Note>(`/v1/ui/notes/${encodeURIComponent(noteId)}`, {
        method: 'PATCH',
        body: input,
        csrfToken: csrf(),
        expectedRevision: input.expected_revision,
      }),

    createImportBatch: (input) =>
      client.request<ImportBatch>('/v1/ui/import-batches', {
        method: 'POST',
        body: input ?? {},
        csrfToken: csrf(),
      }),

    importBatch: (batchId) =>
      client.request<ImportBatch>(`/v1/ui/import-batches/${encodeURIComponent(batchId)}`),

    retryImportItems: (batchId, itemIds) =>
      client.request<{ retried: string[] }>(
        `/v1/ui/import-batches/${encodeURIComponent(batchId)}/retry`,
        { method: 'POST', body: { item_ids: itemIds }, csrfToken: csrf() },
      ),

    cancelImportItem: (batchId, itemId) =>
      client.request<{ item_id: string }>(
        `/v1/ui/import-batches/${encodeURIComponent(batchId)}/items/${encodeURIComponent(itemId)}`,
        { method: 'DELETE', csrfToken: csrf() },
      ),

    uploadFile: (batchId, file, uploadOptions) =>
      client.upload<ImportItemResult>(
        `/v1/ui/import-batches/${encodeURIComponent(batchId)}/files${
          uploadOptions?.idempotencyKey
            ? `?idempotency_key=${encodeURIComponent(uploadOptions.idempotencyKey)}`
            : ''
        }`,
        file,
        { csrfToken: csrf(), signal: uploadOptions?.signal },
      ),

    submitOperation: (request) =>
      client.request<OperationResult>('/v1/ui/operations', {
        method: 'POST',
        body: request,
        csrfToken: csrf(),
        idempotencyKey: request.idempotency_key,
      }),

    reviewQuery: (input) =>
      client.request<PageData<ReviewQueueItem> & { kind: string }>('/v1/ui/review/query', {
        method: 'POST',
        body: input,
      }),

    reviewDecision: (input) =>
      client.request<{
        decision_id: string;
        claim_id: string;
        decision: string;
        created: boolean;
      }>('/v1/ui/review/decisions', {
        method: 'POST',
        body: input,
        csrfToken: csrf(),
        idempotencyKey: input.idempotency_key,
      }),

    createHandoff: (input) =>
      client.request<HandoffManifest>('/v1/ui/handoffs', {
        method: 'POST',
        body: input,
        csrfToken: csrf(),
      }),

    collectionWorkspace: (collectionId) =>
      client.request<CollectionWorkspace>(
        `/v1/ui/collections/${encodeURIComponent(collectionId)}/workspace`,
      ),

    compare: (paperVersionIds) =>
      client.request<Comparison>('/v1/ui/compare', {
        method: 'POST',
        body: { paper_version_ids: paperVersionIds },
      }),

    entitiesQuery: (input) =>
      client.request<PageData<EntityCard> & { kind: string }>('/v1/ui/entities/query', {
        method: 'POST',
        body: input,
      }),

    entity: (entityId) =>
      client.request<EntityCard>(`/v1/ui/entities/${encodeURIComponent(entityId)}`),

    savedSearches: () => client.request<{ items: SavedSearch[] }>('/v1/ui/saved-searches'),

    createSavedSearch: (input) =>
      client.request<SavedSearch>('/v1/ui/saved-searches', {
        method: 'POST',
        body: input,
        csrfToken: csrf(),
      }),

    patchSavedSearch: (id, input) =>
      client.request<SavedSearch>(`/v1/ui/saved-searches/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: input,
        csrfToken: csrf(),
        expectedRevision: input.expected_revision,
      }),

    deleteSavedSearch: (id) =>
      client.request<{ deleted: boolean; target_id: string }>(
        `/v1/ui/saved-searches/${encodeURIComponent(id)}`,
        { method: 'DELETE', csrfToken: csrf() },
      ),

    diagnostics: (input) => client.request('/v1/ui/diagnostics', {
      method: 'POST', body: input, csrfToken: csrf(),
    }),

    tagAction: (input) =>
      client.request<TagActionPreview>('/v1/ui/tags/actions', {
        method: 'POST',
        body: input,
        csrfToken: csrf(),
      }),
  };
}
