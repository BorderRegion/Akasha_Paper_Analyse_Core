/**
 * UX-031 — note autosave, and a 409 that keeps BOTH copies.
 *
 * Requirement (docs/05 §并发编辑 + docs/03 §S04 笔记页): 笔记800ms debounce，blur可提前
 * 保存；每次带expected_revision/If-Match; 409/412保留两份内容供比较，不盲目覆盖；离线草稿
 * 保留本浏览器最多5MiB、可清除；真正持久副本在服务端.
 */

import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp, waitForWorkspace } from '../support/render';
import { NoteAutosave, DRAFT_BUDGET_BYTES, draftKey, readDraft, scopedWorkspaceId, writeDraft } from '../../src/state/notes';
import { ApiError } from '../../src/api/errors';

function conflictError(serverBody: string, currentRevision: number): ApiError {
  return new ApiError({
    code: 'REVISION_CONFLICT',
    message: 'Note changed on the server',
    retryable: false,
    trace_id: 'trc_test',
    details: { server_body: serverBody, current_revision: currentRevision },
    source: 'ui-envelope',
    http_status: 409,
  });
}

describe('UX-031 note autosave debounces, flushes and merges conflicts', () => {
  it('serializes pending edits, switches a created note to PATCH identity, and never saves unchanged text', async () => {
    let complete!: (note: never) => void;
    const save = vi.fn().mockImplementationOnce(() => new Promise(resolve => { complete = resolve; }))
      .mockImplementation(async (id, _body, revision) => ({ note_id: id, revision: revision + 1 }));
    const autosave = new NoteAutosave({ noteId: 'new:paper:version', workspaceId: 'serial',
      storage: localStorage, debounceMs: 5000, save, onChange: () => {} }, { body: '', revision: 0 });
    await autosave.flush();
    expect(save).not.toHaveBeenCalled();
    autosave.onInput('first');
    const pending = autosave.flush();
    autosave.onInput('latest while saving');
    const blur = autosave.flush();
    expect(save).toHaveBeenCalledTimes(1);
    expect(readDraft('serial', 'new:paper:version', localStorage)?.body).toBe('latest while saving');
    complete({ note_id: 'uin_created', revision: 1 } as never);
    await Promise.all([pending, blur]);
    expect(save.mock.calls).toEqual([['new:paper:version', 'first', 0], ['uin_created', 'latest while saving', 1]]);
    await autosave.flush();
    expect(save).toHaveBeenCalledTimes(2);
    expect(autosave.currentState).toBe('saved');
  });

  it('adopting the server copy updates the text and revision, so blur cannot overwrite it', async () => {
    const save = vi.fn().mockRejectedValueOnce(conflictError('server copy', 8))
      .mockResolvedValue({ note_id: 'uin_choose', revision: 9 });
    const autosave = new NoteAutosave({ noteId: 'uin_choose', workspaceId: 'choose',
      save, debounceMs: 5000, onChange: () => {} }, { body: 'old', revision: 1 });
    autosave.onInput('local');
    await autosave.flush();
    autosave.resolveConflictWithServer();
    await autosave.flush();
    expect(save).toHaveBeenCalledTimes(1);
    expect(autosave.pendingBody).toBe('server copy');
    autosave.onInput('server copy edited');
    await autosave.flush();
    expect(save).toHaveBeenLastCalledWith('uin_choose', 'server copy edited', 8);
  });

  it('restores a never-created note draft after reopening and lets the user retry saving', async () => {
    writeDraft(scopedWorkspaceId('local'), 'new:pap_1:pver_pap_1',
      { body: 'offline first note', revision: 0, updatedAt: new Date().toISOString() }, localStorage);
    const server = createFakeServer({ papers: [{ paper_id: 'pap_1', title: 'Paper' }], notes: [] });
    renderApp(server, { path: '/app/papers/pap_1' });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '笔记' }));
    await waitFor(() => expect(screen.getByLabelText('笔记正文')).toHaveValue('offline first note'));
    await user.click(screen.getByRole('button', { name: '重试保存草稿' }));
    await waitFor(() => expect(screen.getByTestId('note-save-state')).toHaveTextContent('已保存'));
    const editor = screen.getByLabelText('笔记正文');
    await user.type(editor, ' continued');
    await user.tab();
    await waitFor(() => expect(server.requests.some(r => r.method === 'PATCH' && r.path.includes('/notes/'))).toBe(true));
    expect(server.requests.filter(r => r.method === 'POST' && r.path === '/v1/ui/notes')).toHaveLength(1);
  });

  it('debounces typing into one save and sends the expected revision', async () => {
    const calls: Array<{ body: string; revision: number }> = [];
    const states: string[] = [];
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_1',
        workspaceId: 'test::local',
        debounceMs: 10,
        storage: window.localStorage,
        save: async (_id, body, revision) => {
          calls.push({ body, revision });
          return { note_id: 'uin_1', revision: revision + 1 } as never;
        },
        onChange: (state) => states.push(state),
      },
      { body: 'initial', revision: 4 },
    );

    autosave.onInput('a');
    autosave.onInput('ab');
    autosave.onInput('abc');
    expect(states).toContain('dirty');
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(calls).toEqual([{ body: 'abc', revision: 4 }]);
    expect(states.at(-1)).toBe('saved');
  });

  it('flushes immediately on blur without waiting for the debounce', async () => {
    const calls: string[] = [];
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_2',
        workspaceId: 'test::local',
        debounceMs: 5000,
        storage: null,
        save: async (_id, body) => {
          calls.push(body);
          return { note_id: 'uin_2', revision: 1 } as never;
        },
        onChange: () => undefined,
      },
      { body: '', revision: 0 },
    );
    autosave.onInput('draft text');
    await autosave.flush();
    expect(calls).toEqual(['draft text']);
  });

  it('keeps both copies on a 409 and reports the conflict', async () => {
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_3',
        workspaceId: 'test::local',
        debounceMs: 5,
        storage: window.localStorage,
        save: async () => {
          throw conflictError('server version of the text', 9);
        },
        onChange: () => undefined,
      },
      { body: 'old', revision: 3 },
    );
    autosave.onInput('my draft');
    await autosave.flush();

    expect(autosave.currentState).toBe('conflict');
    // The local draft is NOT discarded: it is still in the browser.
    const draft = readDraft('test::local', 'uin_3', window.localStorage);
    expect(draft?.body).toBe('my draft');
  });

  it('retries the local copy against the server revision when the user chooses it', async () => {
    const attempts: Array<{ body: string; revision: number }> = [];
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_4',
        workspaceId: 'test::local',
        debounceMs: 5,
        storage: window.localStorage,
        save: async (_id, body, revision) => {
          attempts.push({ body, revision });
          if (attempts.length === 1) throw conflictError('server text', 7);
          return { note_id: 'uin_4', revision: revision + 1 } as never;
        },
        onChange: () => undefined,
      },
      { body: 'old', revision: 2 },
    );
    autosave.onInput('local wording');
    await autosave.flush();
    expect(autosave.currentState).toBe('conflict');

    await autosave.resolveConflictWithLocal();
    expect(attempts[1]).toEqual({ body: 'local wording', revision: 7 });
    expect(autosave.currentState).toBe('saved');
  });

  it('scopes drafts by origin + workspace so two deployments cannot mix', () => {
    const keyA = draftKey('https://a.example::local', 'uin_5');
    const keyB = draftKey('https://b.example::local', 'uin_5');
    expect(keyA).not.toBe(keyB);
  });

  it('refuses a draft that would exceed the browser budget instead of failing silently', () => {
    const storage = window.localStorage;
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_6',
        workspaceId: 'test::local',
        debounceMs: 5,
        storage,
        save: async () => {
          throw new Error('offline');
        },
        onChange: () => undefined,
      },
      { body: '', revision: 0 },
    );
    autosave.onInput('x'.repeat(DRAFT_BUDGET_BYTES + 10));
    // Nothing was written and the state is not silently "saved".
    expect(storage.getItem(draftKey('test::local', 'uin_6'))).toBeNull();
    expect(autosave.currentState).not.toBe('saved');
  });

  it('shows the save state in the editor and surfaces a conflict to the user', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
      notes: [
        {
          note_id: 'uin_conflict',
          paper_id: 'pap_1',
          paper_version_id: 'pver_pap_1',
          claim_id: null,
          body: 'server text',
          revision: 3,
          updated_at: new Date().toISOString(),
        },
      ],
    });
    renderApp(server, { path: '/app/papers/pap_1' });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '笔记' }));

    const editor = await screen.findByLabelText('笔记正文');
    expect(editor).toHaveValue('server text');
    await user.clear(editor);
    await user.type(editor, 'my new draft');
    // Blur flushes instead of waiting for the debounce.
    await user.tab();

    const conflict = await screen.findByTestId('note-conflict');
    expect(within(conflict).getByTestId('conflict-local')).toHaveTextContent('my new draft');
    expect(within(conflict).getByTestId('conflict-server')).toHaveTextContent('server text');
    expect(screen.getByTestId('note-save-state')).toHaveTextContent('冲突');
    expect(screen.queryByText(/不能在此编辑原始证据/)).toBeInTheDocument();
  });

  it('saves a new note with the pinned version as its anchor', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Paper' }],
      modules: [{ id: 'overview', claims: [{ claim_id: 'clm_a', statement: '结论' }] }],
      notes: [],
    });
    renderApp(server, { path: '/app/papers/pap_1' });
    await waitForWorkspace();
    const user = userEvent.setup();
    await user.click(await screen.findByRole('tab', { name: '笔记' }));
    const editor = await screen.findByLabelText('笔记正文');
    await user.type(editor, 'first note');
    await user.tab();

    await waitFor(() => expect(server.requests.some((r) => r.method === 'POST' && r.path === '/v1/ui/notes')).toBe(true));
    const created = server.requests.find((r) => r.method === 'POST' && r.path === '/v1/ui/notes');
    expect(created?.body).toMatchObject({
      paper_id: 'pap_1',
      paper_version_id: 'pver_pap_1',
      body: 'first note',
    });
    await waitFor(() => expect(screen.getByTestId('note-save-state')).toHaveTextContent('已保存'));
  });
});
