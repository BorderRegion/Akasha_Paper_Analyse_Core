/**
 * NotesTab (docs/03 §S04 笔记页 + docs/05 §并发编辑).
 *
 * - user text and model analysis are separated by label, not by colour alone;
 * - the editor autosaves (800 ms debounce, blur flushes), showing
 *   保存中/已保存/失败/冲突;
 * - a 409 keeps BOTH copies and offers an explicit merge — nothing is
 *   overwritten silently;
 * - there is no "edit the original evidence" affordance.
 */

import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import type { Note } from '../../api/contract';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Button } from '../../components/ui/Button';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import styles from './paper.module.css';
import {
  NoteAutosave,
  clearDraft,
  draftStorage,
  readDraft,
  scopedWorkspaceId,
  type ConflictPair,
  type NoteSaveState,
} from '../../state/notes';

export interface NotesTabProps {
  paperId: string;
  paperVersionId: string;
  revision: number;
  workspaceId?: string;
  /** Test seam: skip the real debounce wait. */
  debounceMs?: number;
}

const STATE_LABELS: Record<NoteSaveState, string> = {
  idle: '未修改',
  dirty: '未保存',
  saving: '保存中…',
  saved: '已保存',
  conflict: '存在冲突',
  offline: '离线草稿（未保存到服务器）',
  error: '保存失败',
};

export function NotesTab({
  paperId,
  paperVersionId,
  workspaceId = 'local',
  debounceMs,
}: NotesTabProps): ReactElement {
  const { api, client } = useSession();
  const queryClient = useQueryClient();
  const scoped = useMemo(() => scopedWorkspaceId(workspaceId), [workspaceId]);
  const notesQuery = useQuery({
    queryKey: ['ui', 'notes', paperId, paperVersionId] as const,
    queryFn: async () => (await api.notes(paperId)).data.items,
  });

  const [state, setState] = useState<NoteSaveState>('idle');
  const [conflict, setConflict] = useState<ConflictPair | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [noteId, setNoteId] = useState<string | null>(null);
  const [body, setBody] = useState('');
  const autosaveRef = useRef<NoteAutosave | null>(null);
  const loadedScope = useRef<string | null>(null);

  const existing = useMemo(
    () =>
      (notesQuery.data ?? []).find((note) => note.paper_version_id === paperVersionId && !note.claim_id && !note.evidence_id) ??
      null,
    [notesQuery.data, paperVersionId],
  );

  useEffect(() => {
    if (!notesQuery.isSuccess) return;
    const scopeKey = `${paperId}:${paperVersionId}`;
    if (loadedScope.current === scopeKey) return;
    loadedScope.current = scopeKey;
    autosaveRef.current = null;
    const draft = readDraft(scoped, existing?.note_id ?? `new:${paperId}:${paperVersionId}`, draftStorage())
      ?? readDraft(scoped, `new:${paperId}:${paperVersionId}`, draftStorage());
    setNoteId(existing?.note_id ?? null);
    // A local draft wins the editor on load: it is text the user typed and the
    // server has not accepted yet.
    setBody(draft?.body ?? existing?.body ?? '');
    if (draft && draft.body !== (existing?.body ?? '')) setState('dirty');
  }, [existing, notesQuery.isSuccess, scoped, paperId, paperVersionId]);

  useEffect(
    () => () => {
      void autosaveRef.current?.flush();
    },
    [],
  );

  const ensureAutosave = (): NoteAutosave => {
    if (autosaveRef.current) return autosaveRef.current;
    const draft = readDraft(scoped, noteId ?? `new:${paperId}:${paperVersionId}`, draftStorage())
      ?? readDraft(scoped, `new:${paperId}:${paperVersionId}`, draftStorage());
    const instance = new NoteAutosave(
      {
        noteId: noteId ?? `new:${paperId}:${paperVersionId}`,
        workspaceId: scoped,
        debounceMs,
        storage: draftStorage(),
        save: async (targetId, text, expectedRevision) => {
          if (targetId.startsWith('new:')) {
            const created = await api.createNote({
              paper_id: paperId,
              paper_version_id: paperVersionId,
              body: text,
              paper_wide: true,
            });
            setNoteId(created.data.note_id);
            clearDraft(scoped, `new:${paperId}:${paperVersionId}`, draftStorage());
            queryClient.invalidateQueries({ queryKey: ['ui', 'notes', paperId] });
            return created.data;
          }
          const updated = await api.patchNote(targetId, {
            body: text,
            expected_revision: expectedRevision,
          });
          clearDraft(scoped, `new:${paperId}:${paperVersionId}`, draftStorage());
          queryClient.invalidateQueries({ queryKey: ['ui', 'notes', paperId] });
          return updated.data;
        },
        onChange: (next, detail) => {
          setState(next);
          setConflict(detail?.conflict ?? null);
          setErrorCode(detail?.errorCode ?? null);
        },
      },
      { body: existing?.body ?? '', revision: draft?.revision ?? existing?.revision ?? 0 },
    );
    if (draft && draft.body !== (existing?.body ?? '')) instance.onInput(draft.body);
    autosaveRef.current = instance;
    return instance;
  };

  const dirtyDraftNotice = readDraft(scoped, noteId ?? `new:${paperId}:${paperVersionId}`, draftStorage());

  return (
    <div className={styles.notes} data-testid="notes-tab">
      <div className={styles.noteHeader}>
        <h2>笔记（个人）</h2>
        <span role="status" data-testid="note-save-state">
          {STATE_LABELS[state]}
          {errorCode ? `（${errorCode}）` : ''}
        </span>
      </div>

      <AsyncBoundary
        state={notesQuery.isPending ? 'loading' : notesQuery.isError ? 'error' : 'ready'}
        errorCode={errorCodeOf(notesQuery.error)}
        onRetry={() => void notesQuery.refetch()}
      >
        <textarea
          aria-label="笔记正文"
          className={styles.editor}
          value={body}
          onChange={(event) => {
            setBody(event.target.value);
            ensureAutosave().onInput(event.target.value);
          }}
          onBlur={() => void ensureAutosave().flush()}
        />
      </AsyncBoundary>

      <p className="muted">
        锚点：paper {paperId} · version {paperVersionId}（笔记不会随新版本自动迁移）
      </p>
      {['dirty', 'error', 'offline'].includes(state) ? (
        <Button onClick={() => void ensureAutosave().flush()}>重试保存草稿</Button>
      ) : null}
      {dirtyDraftNotice ? (
        <p className="muted" data-testid="local-draft-note">
          本浏览器存有未保存草稿（{dirtyDraftNotice.updatedAt}），服务器副本不受影响。
        </p>
      ) : null}
      <p className="muted">不能在此编辑原始证据；证据以服务器记录为准。</p>

      {conflict ? (
        <div role="alert" data-testid="note-conflict">
          <p>
            服务器上已有更新（revision {conflict.server.revision}）。两份内容都保留，请选择：
          </p>
          <div className={styles.compare}>
            <div>
              <h3>我的草稿</h3>
              <pre data-testid="conflict-local">{conflict.local.body}</pre>
            </div>
            <div>
              <h3>服务器版本</h3>
              <pre data-testid="conflict-server">{conflict.server.body}</pre>
            </div>
          </div>
          <Button onClick={() => void autosaveRef.current?.resolveConflictWithLocal()}>
            用我的草稿覆盖（以服务器 revision 重试）
          </Button>
          <Button
            variant="quiet"
            onClick={() => {
              autosaveRef.current?.resolveConflictWithServer();
              setBody(conflict.server.body);
            }}
          >
            采用服务器版本
          </Button>
        </div>
      ) : null}

      {client.mode === 'TEST' ? null : null}
    </div>
  );
}

export type { Note };
