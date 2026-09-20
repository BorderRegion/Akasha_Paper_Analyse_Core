/**
 * A deterministic fake `/v1/ui` server for component tests.
 *
 * It implements the documented envelope, cursor semantics and error codes, so a
 * page test exercises the REAL client, the REAL query hooks and the REAL
 * components — only the HTTP transport is replaced. Nothing here is used in
 * production, and a test that needs a failure makes the server fail rather than
 * mocking a component.
 */

import { vi } from 'vitest';
import type { LibraryQuery, PaperListItem } from '../../src/api/contract';
import type { PaperRowPayload } from '../../src/api/ui';

export interface FakePaperInput {
  paper_id: string;
  paper_version_id?: string;
  title: string;
  year?: number | null;
  read_state?: 'UNREAD' | 'READING' | 'READ' | 'LATER';
  saved?: boolean;
  revision?: number;
  page_number?: number | null;
  anchor_version?: string;
  tags?: { id: string; label: string; namespace: string; is_candidate: boolean }[];
  audit?: Record<string, number>;
  tier?: PaperListItem['tier'];
}

export interface FakeModuleInput {
  id: string;
  label?: string;
  availability?: 'AVAILABLE' | 'UNAVAILABLE' | 'DEGRADED';
  reason?: string | null;
  claims?: { claim_id: string; statement: string; claim_type?: string; support_state?: string; evidence_count?: number }[];
}

export interface FakePageEvidenceInput {
  paper_version_id: string;
  document_sha256: string;
  page_number: number;
  page_display_width: number;
  page_display_height: number;
  reference_rotation?: 0 | 90 | 180 | 270;
  evidence: Record<string, unknown>[];
}

export interface FakeServerOptions {
  papers?: FakePaperInput[];
  /** Modules returned by the workspace endpoint (default: empty). */
  modules?: FakeModuleInput[];
  /** Page evidence returned by the page-evidence endpoint, keyed by page. */
  pages?: Record<number, FakePageEvidenceInput>;
  /** Version summaries returned by the workspace endpoint. */
  versions?: { id: string; label: string; is_latest: boolean }[];
  /** Notes returned by GET /v1/ui/papers/{id}/notes. */
  notes?: Record<string, unknown>[];
  /** Claim → evidence rows returned by GET /v1/claims/{id}/evidence. */
  claimEvidence?: Record<string, Record<string, unknown>[]>;
  /** Review queue items returned by POST /v1/ui/review/query. */
  reviewItems?: Record<string, unknown>[];
  /** Fail the next review decision (revision guard). */
  reviewDecisionConflict?: boolean;
  /** Force the handoff endpoint to refuse with a size error. */
  handoffTooLarge?: boolean;
  /** Collection workspace payload for GET /v1/ui/collections/{id}/workspace. */
  collection?: Record<string, unknown>;
  /** Entity cards for /v1/ui/entities/query and /v1/ui/entities/{id}. */
  entities?: Record<string, unknown>[];
  /** Comparison payload for POST /v1/ui/compare. */
  comparison?: Record<string, unknown>;
  /** Saved searches for the saved-search endpoints. */
  savedSearches?: Record<string, unknown>[];
  /** Force a saved-search rename conflict. */
  savedSearchConflict?: boolean;
  /** Tag action response override. */
  tagAction?: Record<string, unknown>;
  /** Operations snapshot payload. */
  snapshot?: Record<string, unknown>;
  /** Core /v1/jobs payload. */
  jobs?: Record<string, unknown>[];
  /** Core /v1/system/status payload. */
  systemStatus?: Record<string, unknown>;
  /** Record diagnostics payloads sent to POST /v1/ui/diagnostics. */
  diagnostics?: Record<string, unknown>[];
  /** Auth is required by default so the session path is exercised. */
  authRequired?: boolean;
  appToken?: string;
  capabilities?: { code: string; availability: 'AVAILABLE' | 'UNAVAILABLE' | 'DEGRADED'; reason: string | null }[];
  limits?: {
    upload_file_bytes: number;
    upload_batch_bytes: number;
    upload_files: number;
    library_page_size: number;
  };
  /** Force a specific response for a path prefix (e.g. 'POST /v1/ui/library/query'). */
  failures?: Record<string, { status: number; code: string; message?: string; retryable?: boolean }>;
}

export interface FakeRequest {
  method: string;
  path: string;
  url: string;
  body: unknown;
  headers: Record<string, string>;
}

function meta() {
  return {
    contract_version: '1.0.0',
    snapshot_id: `snap_${Date.now()}`,
    observed_at: new Date().toISOString(),
    partial: false,
    warnings: [],
  };
}

function envelope(data: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify({ data, meta: meta() }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake', ...headers },
  });
}

function failure(
  status: number,
  code: string,
  message = 'failed',
  retryable = false,
  details: Record<string, unknown> = {},
) {
  return new Response(
    JSON.stringify({ error: { code, message, retryable, trace_id: 'trc_fake', details } }),
    { status, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
  );
}

export function paperFixture(input: FakePaperInput): PaperRowPayload {
  const readState = input.read_state ?? 'UNREAD';
  const anchorPage = input.page_number ?? null;
  return {
    paper_id: input.paper_id,
    paper_version_id: input.paper_version_id ?? `pver_${input.paper_id}`,
    title: input.title,
    authors: {
      value: ['A. Author'],
      missing_reason: null,
      source_refs: [],
    },
    year: {
      value: input.year === undefined ? 2024 : input.year,
      missing_reason: input.year === null ? 'NOT_REPORTED' : null,
      source_refs: [],
    },
    venue: { value: 'Journal of Tests', missing_reason: null, source_refs: [] },
    takeaway: {
      claim_id: `clm_${input.paper_id}`,
      statement: 'Main stored claim',
      claim_type: 'FACT',
      support_state: 'SUPPORTED',
      paper_version_id: input.paper_version_id ?? `pver_${input.paper_id}`,
      evidence_count: 2,
      is_superseded: false,
    },
    tags: input.tags ?? [{ id: 'tag_1', label: 'detection', namespace: 'topic', is_candidate: false }],
    tier: input.tier ?? 'T2_FULL',
    personal: {
      saved: input.saved ?? false,
      read_state: readState,
      revision: input.revision ?? (readState === 'UNREAD' && !input.saved ? 0 : 1),
      reading_anchor: anchorPage
        ? {
            paper_version_id: input.anchor_version ?? input.paper_version_id ?? `pver_${input.paper_id}`,
            page_number: anchorPage,
            section_id: null,
            scroll_offset: null,
          }
        : null,
    },
    audit: {
      total: Object.values(input.audit ?? { SUPPORTED: 2 }).reduce((sum, value) => sum + value, 0),
      by_state: input.audit ?? { SUPPORTED: 2 },
    },
  };
}

export interface FakeServer {
  fetch: typeof fetch;
  requests: FakeRequest[];
  papers: PaperRowPayload[];
  /** Personal-state writes applied by the fake server. */
  personalPatches: { paperId: string; body: Record<string, unknown> }[];
  uploads: { filename: string; size: number; idempotencyKey: string | null }[];
  operations: { kind: string; payload: Record<string, unknown>; idempotencyKey: string | null }[];
  /** Fail the next matching upload by filename. */
  failUploadFor: Set<string>;
  /** Review decisions recorded by the fake server. */
  reviewDecisions: { claim_id: string; decision: string; expected_claim_revision?: string }[];
  /** Handoff requests received by the fake server. */
  handoffs: { paper_version_ids: string[]; include: string[]; max_bytes?: number }[];
  /** Diagnostics payloads received by the fake server. */
  diagnostics: Record<string, unknown>[];
  /** The token the fake server accepts for POST /v1/ui/session. */
  appToken: string;
}

export function createFakeServer(options: FakeServerOptions = {}): FakeServer {
  const papers = (options.papers ?? []).map(paperFixture);
  const requests: FakeRequest[] = [];
  const personalPatches: FakeServer['personalPatches'] = [];
  const uploads: FakeServer['uploads'] = [];
  const operations: FakeServer['operations'] = [];
  const reviewDecisions: FakeServer['reviewDecisions'] = [];
  const handoffs: FakeServer['handoffs'] = [];
  const diagnostics: FakeServer['diagnostics'] = [];
  const failUploadFor = new Set<string>();
  const modules = options.modules ?? [];
  let batchCounter = 0;
  const batches = new Map<string, { batch_id: string; items: unknown[] }>();
  const appToken = options.appToken ?? 'app-token';
  const authRequired = options.authRequired ?? false;
  let authenticated = !authRequired;

  const fetchImpl = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const path = url.replace(/^https?:\/\/[^/]+/, '');
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const rawBody = init?.body;
    let body: unknown = null;
    if (typeof rawBody === 'string') {
      try {
        body = JSON.parse(rawBody);
      } catch {
        body = rawBody;
      }
    }
    requests.push({ method, path, url, body, headers });

    const key = `${method} ${path.split('?')[0]}`;
    const forced = options.failures?.[key];
    if (forced) return failure(forced.status, forced.code, forced.message, forced.retryable);

    const requiresAuth = !path.startsWith('/v1/ui/bootstrap') && key !== 'POST /v1/ui/session';
    if (authRequired && requiresAuth && !authenticated) {
      const bearer = headers.Authorization;
      if (!bearer) return failure(401, 'AUTH_001', 'authentication required');
    }

    if (key === 'GET /v1/ui/bootstrap') {
      return envelope({
        app_version: '1.0.0',
        ui_contract_version: '1.0.0',
        auth_required: authRequired,
        mode: 'TEST',
      });
    }
    if (key === 'POST /v1/ui/session' || key === 'GET /v1/ui/session') {
      authenticated = true;
      return envelope({
        authenticated: true,
        csrf_token: 'csrf-from-server',
        expires_at: new Date(Date.now() + 3600_000).toISOString(),
      });
    }
    if (key === 'DELETE /v1/ui/session') {
      authenticated = false;
      return envelope({ authenticated: false, revoked: true, csrf_token: null, clear_client_caches: true });
    }
    if (key === 'GET /v1/ui/capabilities') {
      return envelope({
        ui_contract_version: '1.0.0',
        capabilities:
          options.capabilities ??
          ['library.query', 'library.pagination', 'import.upload', 'personal.state'].map((code) => ({
            code,
            availability: 'AVAILABLE' as const,
            reason: null,
          })),
        limits:
          options.limits ??
          {
            upload_file_bytes: 100 * 1024 * 1024,
            upload_batch_bytes: 500 * 1024 * 1024,
            upload_files: 50,
            library_page_size: 50,
          },
        mode: 'TEST',
      });
    }
    if (key === 'POST /v1/ui/library/query') {
      const query = (body ?? {}) as LibraryQuery;
      const filtered = papers.filter((paper) => {
        const filters = query.filters ?? {};
        if (filters.read_states?.length && !filters.read_states.includes(paper.personal.read_state)) {
          return false;
        }
        if (filters.audit_states?.length) {
          const states = Object.keys(paper.audit.by_state);
          if (!filters.audit_states.some((state) => states.includes(state))) return false;
        }
        if (query.query && !paper.title.toLowerCase().includes(query.query.toLowerCase())) return false;
        return true;
      });
      const offset = query.cursor ? Number(query.cursor) : 0;
      const limit = query.limit ?? 50;
      const slice = filtered.slice(offset, offset + limit);
      const hasMore = offset + limit < filtered.length;
      const page = {
        items: query.kind === 'CLAIMS' ? slice.map((paper) => ({
          claim_id: paper.takeaway?.claim_id ?? `clm_${paper.paper_id}`,
          statement: paper.takeaway?.statement ?? `结论：${paper.title}`,
          claim_type: paper.takeaway?.claim_type ?? 'INFERENCE',
          support_state: paper.takeaway?.support_state ?? 'UNVERIFIED',
          paper_id: paper.paper_id, paper_version_id: paper.paper_version_id,
          evidence_count: paper.takeaway?.evidence_count ?? 0,
        })) : query.kind === 'TECHNIQUES' ? (options.entities ?? []).map((entity) => ({
          entity_id: entity.entity_id, entity_type: entity.entity_type,
          name: entity.name, aliases: entity.aliases,
        })) : slice,
        next_cursor: hasMore ? String(offset + limit) : null,
        has_more: hasMore,
        total: filtered.length,
        total_kind: 'EXACT',
        scope_revision: 'scope-1',
        kind: query.kind ?? 'PAPERS',
      };
      return envelope(page);
    }
    if (key.startsWith('PATCH ') && key.endsWith('/personal')) {
      const paperId = path.split('/')[4] ?? '';
      const patch = (body ?? {}) as Record<string, unknown>;
      personalPatches.push({ paperId, body: patch });
      const paper = papers.find((candidate) => candidate.paper_id === paperId);
      if (paper) {
        if (typeof patch.saved === 'boolean') paper.personal.saved = patch.saved;
        if (patch.reading_anchor) paper.personal.reading_anchor = patch.reading_anchor as PaperRowPayload['personal']['reading_anchor'];
        if (typeof patch.read_state === 'string') {
          paper.personal.read_state = patch.read_state as PaperRowPayload['personal']['read_state'];
        }
        paper.personal.revision += 1;
      }
      return envelope(paper?.personal ?? { saved: false, read_state: 'UNREAD', revision: 1, reading_anchor: null });
    }
    if (key === 'POST /v1/ui/import-batches') {
      batchCounter += 1;
      const batchId = `uib_fake_${batchCounter}`;
      batches.set(batchId, { batch_id: batchId, items: [] });
      return envelope({ batch_id: batchId, items: [] });
    }
    if (key.startsWith('GET /v1/ui/import-batches/')) {
      const batchId = path.split('/')[4] ?? '';
      return envelope(batches.get(batchId) ?? { batch_id: batchId, items: [] });
    }
    if (key.startsWith('POST ') && key.endsWith('/files')) {
      const batchId = path.split('/')[4] ?? '';
      const batch = batches.get(batchId) ?? { batch_id: batchId, items: [] };
      batches.set(batchId, batch);
      const form = rawBody as FormData | undefined;
      const file = form?.get?.('file') as File | null;
      const filename = file?.name ?? 'unknown.pdf';
      const idempotencyKey = new URLSearchParams(path.split('?')[1] ?? '').get('idempotency_key');
      uploads.push({ filename, size: file?.size ?? 0, idempotencyKey });
      if (failUploadFor.has(filename)) {
        return failure(400, 'PDF_001', 'The uploaded file is not a PDF (magic bytes mismatch).');
      }
      const item = {
        item_id: `uit_${filename}`,
        filename,
        size_bytes: file?.size ?? 0,
        state: 'IMPORTED',
        paper_id: `pap_${filename}`,
        paper_version_id: `pver_${filename}`,
        job_id: `job_${filename}`,
        error_code: null,
      };
      batch.items.push(item);
      return envelope({
        batch_id: batchId,
        item_id: item.item_id,
        state: item.state,
        sha256: 'a'.repeat(64),
        size_bytes: item.size_bytes,
        created: true,
        paper_id: item.paper_id,
        paper_version_id: item.paper_version_id,
        job_id: item.job_id,
        error_code: null,
      });
    }
    if (key === 'POST /v1/ui/operations') {
      const request = (body ?? {}) as { kind: string; payload?: Record<string, unknown> };
      operations.push({
        kind: request.kind,
        payload: request.payload ?? {},
        idempotencyKey: headers['Idempotency-Key'] ?? null,
      });
      return new Response(
        JSON.stringify({
          data: {
            operation_id: `uop_${operations.length}`,
            kind: request.kind,
            state: 'ACCEPTED',
            scope: { paper_ids: (request.payload?.paper_ids as string[]) ?? [] },
            job_ids: [],
            results: (((request.payload?.paper_ids as string[]) ?? []) as string[]).map((id) => ({
              target_id: id,
              status: 'COMPLETED',
              error_code: null,
            })),
            error_code: null,
          },
          meta: meta(),
        }),
        { status: 202, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
      );
    }
    if (key.startsWith('GET /v1/ui/papers/') && key.endsWith('/workspace')) {
      const paperId = path.split('/')[4] ?? '';
      const paper = papers.find((candidate) => candidate.paper_id === paperId);
      const requested = new URLSearchParams(path.split('?')[1] ?? '').get('paper_version_id');
      const versionId = requested ?? paper?.paper_version_id ?? 'pver_unknown';
      return envelope({
        paper: paper ?? paperFixture({ paper_id: paperId, title: 'Unknown' }),
        document_sha256: options.pages?.[1]?.document_sha256 ?? 'b'.repeat(64),
        selected_version_id: versionId,
        versions: options.versions ?? [
          { id: versionId, label: 'v1', is_latest: true },
        ],
        modules: modules.map((module) => ({
          id: module.id,
          label: module.label ?? module.id,
          availability: module.availability ?? 'AVAILABLE',
          reason: module.reason ?? null,
          claims: (module.claims ?? []).map((claim) => ({
            claim_id: claim.claim_id,
            statement: claim.statement,
            claim_type: claim.claim_type ?? 'FACT',
            support_state: claim.support_state ?? 'SUPPORTED',
            paper_version_id: versionId,
            evidence_count: claim.evidence_count ?? 1,
            is_superseded: false,
          })),
        })),
        analysis_revision: 'rev-1',
        personal: paper?.personal ?? { saved: false, read_state: 'UNREAD', revision: 0, reading_anchor: null },
        audit: paper?.audit ?? { total: 0, by_state: {} },
      });
    }
    if (key.includes('/pages/') && key.endsWith('/evidence')) {
      const pageNumber = Number(path.split('/pages/')[1]?.split('/')[0] ?? '1');
      const fallback: FakePageEvidenceInput = options.pages?.[pageNumber] ?? {
        paper_version_id: 'pver_unknown',
        document_sha256: 'b'.repeat(64),
        page_number: pageNumber,
        page_display_width: 612,
        page_display_height: 792,
        evidence: [],
      };
      return envelope({ reference_rotation: 0, ...fallback, page_number: pageNumber });
    }
    if (key.startsWith('GET /v1/ui/paper-versions/') && key.endsWith('/document')) {
      // A tiny placeholder PDF body: the engine is injected in tests, so these
      // bytes only have to exist.
      return new Response(new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d]), {
        status: 200,
        headers: { 'Content-Type': 'application/pdf', 'X-Trace-ID': 'trc_fake' },
      });
    }
    if (key === 'GET /v1/ui/operations/snapshot') {
      return envelope(
        options.snapshot ?? {
          queue: { pending: 0, running: 0, failed: 0 },
          workers: [{ identity: '(none)', last_seen: null, state: 'UNOBSERVED' }],
          modules: [],
          providers: [],
          disk: [],
          eta_available: false,
          observed_at: new Date().toISOString(),
          unknown_reasons: [],
        },
      );
    }
    if (key === 'GET /v1/ui/jobs') {
      return envelope({ jobs: options.jobs ?? [] });
    }
    if (key === 'GET /v1/ui/system/status') {
      return envelope(options.systemStatus ?? { modules: [], providers: {} });
    }
    if (key === 'POST /v1/ui/diagnostics') {
      const input = (body ?? {}) as Record<string, unknown>;
      diagnostics.push(input);
      return envelope({
        asset_id: 'ui-diagnostics-test.json',
        path: '/tmp/ui-diagnostics-test.json',
        size_bytes: 2048,
        max_bytes: 2 * 1024 * 1024,
        redacted_fields: ['authorization', 'cookie', 'token', 'api_key'],
        manifest: {
          included: {
            request_bodies: false,
            notes: false,
            search_text: false,
            credentials: false,
            evidence_excerpt: Boolean(input.include_evidence_excerpt),
          },
        },
      });
    }
    if (key.startsWith('GET /v1/ui/collections/') && key.endsWith('/workspace')) {
      const collectionId = path.split('/')[4] ?? '';
      return envelope(
        options.collection ?? {
          collection_id: collectionId,
          title: 'Collection',
          selection_hash: 'sel-0',
          selected_paper_count: 0,
          analyzed_paper_count: 0,
          analysis_revision: 'rev-0',
          sample: { paper_versions: 0, claims: 0, years: null },
          insights: [],
        },
      );
    }
    if (key === 'POST /v1/ui/compare') {
      const input = (body ?? {}) as { paper_version_ids: string[] };
      return envelope(
        options.comparison ?? {
          paper_version_ids: input.paper_version_ids,
          selection_hash: 'sel-cmp',
          analysis_revision: 'rev-cmp',
          cells: [],
          papers: Object.fromEntries(
            input.paper_version_ids.map((id) => [
              id,
              {
                paper_id: `pap_${id}`,
                version_label: 'v1',
                document_sha256: 'd'.repeat(64),
                publication_date: null,
              },
            ]),
          ),
        },
      );
    }
    if (key === 'POST /v1/ui/entities/query') {
      const input = (body ?? {}) as { query?: string };
      const all = options.entities ?? [];
      const filtered = input.query
        ? all.filter((entity) => JSON.stringify(entity).includes(String(input.query)))
        : all;
      return envelope({
        items: filtered,
        next_cursor: null,
        has_more: false,
        total: filtered.length,
        total_kind: 'EXACT',
        scope_revision: 'scope-1',
        kind: 'ENTITIES',
      });
    }
    if (key.startsWith('GET /v1/ui/entities/')) {
      const entityId = path.split('/')[4] ?? '';
      const entity = (options.entities ?? []).find((item) => item.entity_id === entityId);
      if (!entity) return failure(404, 'GRAPH_001', 'Unknown entity');
      return envelope(entity);
    }
    if (key === 'GET /v1/ui/saved-searches') {
      return envelope({ items: options.savedSearches ?? [] });
    }
    if (key === 'POST /v1/ui/saved-searches') {
      const input = (body ?? {}) as { name: string; query: Record<string, unknown> };
      return envelope({
        saved_search_id: 'uss_1',
        name: input.name,
        query: input.query,
        revision: 1,
      });
    }
    if (key.startsWith('PATCH /v1/ui/saved-searches/')) {
      const id = path.split('/')[4] ?? '';
      const input = (body ?? {}) as { name?: string; expected_revision?: number };
      if (options.savedSearchConflict) {
        return failure(409, 'REVISION_CONFLICT', 'Saved search changed elsewhere', false, {
          expected_revision: input.expected_revision ?? 0,
          current_revision: (input.expected_revision ?? 0) + 1,
        });
      }
      return envelope({
        saved_search_id: id,
        name: input.name ?? 'renamed',
        query: {},
        revision: (input.expected_revision ?? 0) + 1,
      });
    }
    if (key === 'POST /v1/ui/tags/actions') {
      const input = (body ?? {}) as { action: string; tag_ids: string[]; preview_only?: boolean };
      return envelope(
        options.tagAction ?? {
          operation_id: 'uop_tags',
          action: input.action,
          state: input.preview_only === false ? 'COMPLETED' : 'PREVIEW',
          affected_count: 3,
          affected_paper_ids: ['pap_1', 'pap_2', 'pap_3'],
          tags: input.tag_ids,
          target_tag_id: input.tag_ids[0] ?? null,
          conflict: null,
          note: '预览：未做任何修改；确认后再执行。',
        },
      );
    }
    if (key === 'POST /v1/ui/review/query') {
      const input = (body ?? {}) as { include_seen?: boolean; include_all?: boolean; groups?: string[] };
      const all = options.reviewItems ?? [];
      const filtered = all.filter((item) => {
        const record = item as { personal_decision?: string | null; group?: string; impact?: string };
        if (!input.include_seen && record.personal_decision === 'SEEN') return false;
        if (!input.include_all && record.impact !== 'HIGH') return false;
        if (input.groups?.length && !input.groups.includes(String(record.group))) return false;
        return true;
      });
      return envelope({
        items: filtered,
        next_cursor: null,
        has_more: false,
        total: filtered.length,
        total_kind: 'EXACT',
        scope_revision: 'scope-1',
        kind: 'REVIEW_ITEMS',
      });
    }
    if (key === 'POST /v1/ui/review/decisions') {
      const input = (body ?? {}) as {
        claim_id: string;
        decision: string;
        expected_claim_revision?: string;
      };
      if (options.reviewDecisionConflict) {
        return failure(409, 'REVISION_CONFLICT', 'This claim changed since you opened it', false, {
          expected_claim_revision: input.expected_claim_revision ?? null,
          current_revision: 'changed',
        });
      }
      reviewDecisions.push(input);
      return envelope({
        decision_id: `urd_${reviewDecisions.length}`,
        claim_id: input.claim_id,
        decision: input.decision,
        created: true,
      });
    }
    if (key === 'POST /v1/ui/handoffs') {
      const input = (body ?? {}) as {
        paper_version_ids: string[];
        include: string[];
        max_bytes?: number;
      };
      handoffs.push(input);
      if (options.handoffTooLarge) {
        return failure(413, 'STORAGE_001', 'The handoff is above max_bytes', true, {
          size_bytes: 4000,
          max_bytes: input.max_bytes ?? 0,
        });
      }
      const manifest = {
        schema_version: '1.0.0',
        generated_at: new Date().toISOString(),
        include: [...input.include].sort(),
        scope: { paper_version_ids: input.paper_version_ids },
        papers: input.paper_version_ids.map((id) => ({
          paper_id: `pap_${id}`,
          paper_version_id: id,
          source_hashes: { document_sha256: 'f'.repeat(64) },
          claims: [],
        })),
      };
      return new Response(
        JSON.stringify({
          data: {
            asset_id: 'ho_fake_1',
            size_bytes: 2048,
            expires_at: null,
            redacted_fields: [],
            manifest,
          },
          meta: meta(),
        }),
        { status: 202, headers: { 'Content-Type': 'application/json', 'X-Trace-ID': 'trc_fake' } },
      );
    }
    if (key.startsWith('GET /v1/ui/claims/') && key.endsWith('/evidence')) {
      const claimId = path.split('/')[4] ?? '';
      return envelope({ claim_id: claimId, evidence: options.claimEvidence?.[claimId] ?? [] });
    }
    if (key.startsWith('POST /v1/ui/notes')) {
      const input = (body ?? {}) as { body?: string; paper_id?: string; paper_version_id?: string };
      return envelope({
        note_id: 'uin_created',
        paper_id: input.paper_id ?? '',
        paper_version_id: input.paper_version_id ?? '',
        claim_id: null,
        body: input.body ?? '',
        revision: 1,
        updated_at: new Date().toISOString(),
      });
    }
    if (key.startsWith('PATCH /v1/ui/notes/')) {
      const noteId = path.split('/')[4] ?? '';
      const input = (body ?? {}) as { body?: string; expected_revision?: number };
      const forcedConflict = options.failures?.['PATCH /v1/ui/notes/conflict'];
      if (forcedConflict || noteId === 'uin_conflict') {
        const serverBody =
          (options.notes ?? []).find((note) => note.note_id === noteId)?.body ?? '';
        // Mirrors the real backend: the server copy and its revision come back
        // so the client can show both instead of overwriting.
        return failure(409, 'REVISION_CONFLICT', 'Note changed on the server', false, {
          server_body: serverBody,
          current_revision: Number(
            (options.notes ?? []).find((note) => note.note_id === noteId)?.revision ?? 0,
          ),
        });
      }
      return envelope({
        note_id: noteId,
        paper_id: '',
        paper_version_id: '',
        claim_id: null,
        body: input.body ?? '',
        revision: (input.expected_revision ?? 0) + 1,
        updated_at: new Date().toISOString(),
      });
    }
    if (key.startsWith('GET /v1/ui/papers/') && key.endsWith('/notes')) {
      return envelope({ items: options.notes ?? [] });
    }

    return failure(404, 'STORAGE_003', `unhandled fake route ${key}`);
  });

  return {
    appToken,
    fetch: fetchImpl as unknown as typeof fetch,
    requests,
    papers,
    personalPatches,
    uploads,
    operations,
    failUploadFor,
    reviewDecisions,
    handoffs,
    diagnostics,
  };
}

export { failure as failureResponse, envelope as envelopeResponse };
