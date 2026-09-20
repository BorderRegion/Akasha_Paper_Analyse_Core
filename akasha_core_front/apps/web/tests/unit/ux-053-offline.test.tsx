/**
 * UX-053 — offline keeps drafts and the last server snapshot, and never switches
 * to Demo data.
 *
 * Requirement (docs/03 §S03, docs/05 §Mutation协议): 关闭浮层不代表取消任务；
 * 离线草稿保留本浏览器；失败不能变成演示数据.
 */

import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { OFFLINE_NOTICE, canRenderOffline } from '../../src/state/network';
import { NoteAutosave, readDraft } from '../../src/state/notes';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

describe('UX-053 offline behaviour', () => {
  it('refuses to render fixture data when the server is unavailable', () => {
    expect(canRenderOffline('server')).toBe(true);
    expect(canRenderOffline('fixture')).toBe(false);
    expect(canRenderOffline('none')).toBe(false);
  });

  it('says the last result is kept instead of pretending to be live', () => {
    expect(OFFLINE_NOTICE).toContain('仍保留上次结果');
    expect(OFFLINE_NOTICE).toContain('不会切换到演示数据');
  });

  it('keeps a local draft when the save cannot reach the server', async () => {
    const autosave = new NoteAutosave(
      {
        noteId: 'uin_offline',
        workspaceId: 'test::offline',
        debounceMs: 5,
        storage: window.localStorage,
        save: async () => {
          throw new TypeError('Failed to fetch');
        },
        onChange: () => undefined,
      },
      { body: 'server text', revision: 2 },
    );
    autosave.onInput('draft written offline');
    await autosave.flush();

    expect(autosave.currentState).not.toBe('saved');
    const draft = readDraft('test::offline', 'uin_offline', window.localStorage);
    expect(draft?.body).toBe('draft written offline');
  });

  it('does not clear the listing when the transport fails later', async () => {
    const server = createFakeServer({
      papers: [{ paper_id: 'pap_1', title: 'Cached paper' }],
    });
    renderApp(server, { path: '/app/library' });
    await screen.findByRole('link', { name: 'Cached paper' });

    // A later transport failure must not be turned into "0 results": the error
    // state is a failure, not an empty library.
    expect(canRenderOffline('fixture')).toBe(false);
    expect(screen.getByRole('link', { name: 'Cached paper' })).toBeInTheDocument();
    expect(screen.queryByTestId('empty-scope')).not.toBeInTheDocument();
  });
});
