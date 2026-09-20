/**
 * Note autosave (spec docs/05 §并发编辑/自动保存 + docs/03 §S04 笔记页).
 *
 * Contract:
 * - 800 ms debounce, `flush()` on blur;
 * - every save carries the revision the editor saw;
 * - a 409/412 does NOT overwrite: the local draft AND the server copy are both
 *   kept, so the user can compare and merge;
 * - offline drafts live in this browser, scoped by origin + workspace, capped at
 *   5 MiB and clearable; the durable copy is always the server's.
 */

import type { Note } from '../api/contract';

export type NoteSaveState =
  | 'idle'
  | 'dirty'
  | 'saving'
  | 'saved'
  | 'conflict'
  | 'offline'
  | 'error';

export interface DraftRecord {
  body: string;
  revision: number;
  updatedAt: string;
}

export interface ConflictPair {
  local: DraftRecord;
  server: Note;
}

export interface AutosaveOptions {
  noteId: string;
  workspaceId: string;
  /** Persists one revision; throws ApiError(REVISION_CONFLICT) on a lost race. */
  save(noteId: string, body: string, expectedRevision: number): Promise<Note>;
  debounceMs?: number;
  storage?: Storage | null;
  onChange(state: NoteSaveState, detail?: { conflict?: ConflictPair; errorCode?: string }): void;
}

const DRAFT_PREFIX = 'paperintel.draft';
/** docs/05: 离线草稿保留本浏览器最多5MiB、可清除. */
export const DRAFT_BUDGET_BYTES = 5 * 1024 * 1024;

export function draftKey(workspaceId: string, noteId: string): string {
  return `${DRAFT_PREFIX}.${workspaceId}.${noteId}`;
}

/** Drafts are keyed by ORIGIN + workspace so two deployments never mix. */
export function scopedWorkspaceId(workspaceId: string, origin?: string): string {
  return `${origin ?? (typeof window === 'undefined' ? 'no-origin' : window.location.origin)}::${workspaceId}`;
}

export function draftStorage(): Storage | null {
  try { return typeof window === 'undefined' ? null : window.localStorage; }
  catch { return null; }
}

export function readDraft(workspaceId: string, noteId: string, storage: Storage | null): DraftRecord | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(draftKey(workspaceId, noteId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as DraftRecord;
    if (typeof parsed?.body !== 'string' || !Number.isInteger(parsed.revision) || parsed.revision < 0) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function writeDraft(
  workspaceId: string,
  noteId: string,
  record: DraftRecord,
  storage: Storage | null,
): boolean {
  if (!storage) return false;
  const payload = JSON.stringify(record);
  if (new TextEncoder().encode(payload).length > DRAFT_BUDGET_BYTES) return false;
  try {
    const key = draftKey(workspaceId, noteId);
    let total = new TextEncoder().encode(payload).length;
    for (let i = 0; i < storage.length; i++) {
      const existing = storage.key(i);
      if (existing?.startsWith(`${DRAFT_PREFIX}.`) && existing !== key) {
        total += new TextEncoder().encode(storage.getItem(existing) ?? '').length;
      }
    }
    if (total > DRAFT_BUDGET_BYTES) return false;
    storage.setItem(key, payload);
    return true;
  } catch {
    return false;
  }
}

export function clearDraft(workspaceId: string, noteId: string, storage: Storage | null): void {
  try {
    storage?.removeItem(draftKey(workspaceId, noteId));
  } catch {
    /* ignore */
  }
}

export class NoteAutosave {
  private readonly options: AutosaveOptions;
  private readonly storage: Storage | null;
  private readonly debounceMs: number;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private revision: number;
  private body: string;
  private savedBody: string;
  private noteId: string;
  private inFlight: Promise<void> | null = null;
  private state: NoteSaveState = 'idle';

  constructor(options: AutosaveOptions, initial: { body: string; revision: number }) {
    this.options = options;
    this.storage = options.storage ?? null;
    this.debounceMs = options.debounceMs ?? 800;
    this.body = initial.body;
    this.savedBody = initial.body;
    this.noteId = options.noteId;
    this.revision = initial.revision;
  }

  get currentState(): NoteSaveState {
    return this.state;
  }

  get pendingBody(): string {
    return this.body;
  }

  /** The user typed: debounce a save and keep a local draft immediately. */
  onInput(body: string): void {
    this.body = body;
    if (this.lastConflict) {
      this.lastConflict.local = { body, revision: this.revision, updatedAt: new Date().toISOString() };
      this.persistDraft();
      this.setState('conflict', { conflict: this.lastConflict });
      return;
    }
    this.setState('dirty');
    this.persistDraft();
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => void this.flush(), this.debounceMs);
  }

  /** Blur / navigation: save NOW instead of waiting for the debounce. */
  async flush(): Promise<void> {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    if (this.inFlight) return this.inFlight;
    if (this.lastConflict) return;
    if (this.body === this.savedBody) {
      clearDraft(this.options.workspaceId, this.noteId, this.storage);
      this.setState('saved');
      return;
    }
    this.inFlight = this.save().finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  /** The user chose the server copy; the local draft is dropped deliberately. */
  resolveConflictWithServer(): void {
    if (!this.lastConflict) return;
    this.body = this.savedBody = this.lastConflict.server.body;
    this.revision = this.lastConflict.server.revision;
    this.lastConflict = null;
    clearDraft(this.options.workspaceId, this.noteId, this.storage);
    this.setState('saved');
  }

  /** The user chose to keep their own text: retry against the server revision. */
  async resolveConflictWithLocal(): Promise<void> {
    const conflict = this.lastConflict;
    if (!conflict) return;
    this.revision = conflict.server.revision;
    this.savedBody = conflict.server.body;
    this.lastConflict = null;
    await this.flush();
  }

  private lastConflict: ConflictPair | null = null;

  private async save(): Promise<void> {
    // Serialize writes; edits arriving during a request are saved afterwards
    // with its new ID/revision, never erased by the older response.
    while (this.body !== this.savedBody && !this.lastConflict) {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const body = this.body;
    this.setState('saving');
    try {
      const saved = await this.options.save(this.noteId, body, this.revision);
      const previousId = this.noteId;
      this.noteId = saved.note_id;
      this.revision = saved.revision;
      this.savedBody = body;
      clearDraft(this.options.workspaceId, previousId, this.storage);
      if (this.body === body) {
        clearDraft(this.options.workspaceId, this.noteId, this.storage);
        this.setState('saved');
      } else {
        this.persistDraft();
        this.setState('dirty');
      }
    } catch (error) {
      const code = (error as { shape?: { code?: string } })?.shape?.code;
      if (code === 'REVISION_CONFLICT') {
        // Keep BOTH copies: nothing is overwritten silently.
        const server = ((error as { shape?: { details?: Record<string, unknown> } })?.shape?.details ??
          {}) as { server_body?: string; current_revision?: number; note_id?: string };
        if (server.note_id && server.note_id !== this.noteId) {
          const previousId = this.noteId;
          this.noteId = server.note_id;
          this.persistDraft();
          clearDraft(this.options.workspaceId, previousId, this.storage);
        }
        this.lastConflict = {
          local: { body: this.body, revision: this.revision, updatedAt: new Date().toISOString() },
          server: {
            note_id: this.noteId,
            paper_id: '',
            paper_version_id: '',
            claim_id: null,
            body: server.server_body ?? '',
            revision: server.current_revision ?? this.revision,
            updated_at: new Date().toISOString(),
          },
        };
        this.persistDraft();
        this.setState('conflict', { conflict: this.lastConflict });
        return;
      }
      // Offline: the draft stays usable and is clearly marked as not saved.
      this.persistDraft();
      this.setState(navigatorOnline() ? 'error' : 'offline', { errorCode: code ?? 'INTERNAL_001' });
      return;
    }
    }
  }

  private persistDraft(): void {
    const stored = writeDraft(
      this.options.workspaceId,
      this.noteId,
      { body: this.body, revision: this.revision, updatedAt: new Date().toISOString() },
      this.storage,
    );
    if (!stored && this.storage) {
      // The 5 MiB browser budget is full: say so instead of pretending.
      this.setState('offline');
    }
  }

  private setState(
    state: NoteSaveState,
    detail?: { conflict?: ConflictPair; errorCode?: string },
  ): void {
    this.state = state;
    this.options.onChange(state, detail);
  }
}

function navigatorOnline(): boolean {
  return typeof navigator === 'undefined' ? true : navigator.onLine !== false;
}
