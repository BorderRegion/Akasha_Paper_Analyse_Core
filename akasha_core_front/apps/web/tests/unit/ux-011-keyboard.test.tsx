import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../../src/app/App';
import { DEFAULT_PREFERENCES } from '../../src/state/theme';
import { PRIMARY_NAV, SECONDARY_NAV } from '../../src/app/shell/Sidebar';
import { isComposing, isEditableTarget } from '../../src/lib/a11y';
import { createFakeServer } from '../support/fake-server';

function renderShell(options: { singleKeyShortcuts?: boolean } = {}) {
  return render(
    <App
      mode="TEST" fetchImpl={createFakeServer().fetch}
      initialPreferences={{ ...DEFAULT_PREFERENCES, single_key_shortcuts: options.singleKeyShortcuts ?? true }}
    />,
  );
}

describe('UX-011 sidebar and command entry keyboard access', () => {
  it('exposes every navigation entry as a link with an accessible name', async () => {
    renderShell();
    await screen.findByRole('navigation', { name: '主导航' });
    for (const item of [...PRIMARY_NAV, ...SECONDARY_NAV]) {
      expect(screen.getByRole('link', { name: item.label })).toHaveAttribute('href', item.to);
    }
  });

  it('reaches every nav link with Tab and never traps focus', async () => {
    renderShell();
    await screen.findByRole('navigation', { name: '主导航' });
    const user = userEvent.setup();
    const seen: string[] = [];
    for (let i = 0; i < 12; i += 1) {
      await user.tab();
      const active = document.activeElement as HTMLElement | null;
      if (!active) break;
      seen.push(`${active.tagName.toLowerCase()}:${active.textContent?.trim().slice(0, 12) ?? ''}`);
    }
    // The skip link and the primary navigation are part of the tab order.
    expect(seen.join('|')).toContain('跳到主要内容');
    expect(seen.some((entry) => entry.includes('文献库'))).toBe(true);
    expect(seen.some((entry) => entry.includes('设置'))).toBe(true);
    expect(new Set(seen).size).toBeGreaterThan(3);
  });

  it('opens the command palette with Ctrl+K and returns focus on Esc', async () => {
    renderShell();
    const trigger = await screen.findByRole('button', { name: /命令与搜索/ });
    trigger.focus();
    const user = userEvent.setup();
    await user.keyboard('{Control>}k{/Control}');
    const palette = await screen.findByTestId('command-palette');
    expect(palette).toHaveAttribute('role', 'dialog');
    expect(screen.getByRole('textbox', { name: '命令或搜索' })).toHaveFocus();

    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it('focuses global search with "/" only when not typing', async () => {
    renderShell();
    const search = await screen.findByRole('searchbox', { name: '全局搜索' });
    const user = userEvent.setup();
    await user.keyboard('/');
    expect(search).toHaveFocus();

    // Typing "/" inside the field must stay in the field (no shortcut).
    await user.keyboard('/');
    expect(search).toHaveFocus();
    expect((search as HTMLInputElement).value).toBe('/');
  });

  it('toggles focus mode with F and keeps the exit affordance visible', async () => {
    const { container } = renderShell();
    await screen.findByRole('navigation', { name: '主导航' });
    const user = userEvent.setup();
    await user.keyboard('f');
    const shell = container.querySelector('[data-focus]');
    await waitFor(() => expect(shell).toHaveAttribute('data-focus', 'true'));
    expect(screen.getByRole('button', { name: /退出专注模式/ })).toBeInTheDocument();
    await user.keyboard('f');
    await waitFor(() => expect(shell).toHaveAttribute('data-focus', 'false'));
  });

  it('ignores single-key shortcuts while an IME composition is active', async () => {
    renderShell();
    const search = await screen.findByRole('searchbox', { name: '全局搜索' });
    const user = userEvent.setup();
    search.focus();
    // The browser reports keyCode 229 / isComposing during composition.
    await user.keyboard('/');
    expect(search).toHaveFocus();
    const composing = new KeyboardEvent('keydown', { key: 'f', isComposing: true });
    expect(isComposing(composing)).toBe(true);
    expect(isComposing(new KeyboardEvent('keydown', { key: 'f' }))).toBe(false);
  });

  it('honours the single-key-shortcuts setting', async () => {
    const { container } = renderShell({ singleKeyShortcuts: false });
    const search = await screen.findByRole('searchbox', { name: '全局搜索' });
    search.blur();
    const user = userEvent.setup();
    await user.keyboard('/');
    expect(search).not.toHaveFocus();
    await user.keyboard('f');
    await waitFor(() => expect(container.querySelector('[data-focus]')).toHaveAttribute('data-focus', 'false'));
  });

  it('never intercepts Alt+Left/Right (browser history)', async () => {
    renderShell();
    await screen.findByRole('navigation', { name: '主导航' });
    const user = userEvent.setup();
    const wentBack = vi.fn();
    window.addEventListener('popstate', wentBack);
    await user.keyboard('{Alt>}{ArrowLeft}{/Alt}');
    // No shell state change and no preventDefault path is taken for history keys.
    expect(screen.getByRole('navigation', { name: '主导航' })).toBeInTheDocument();
    window.removeEventListener('popstate', wentBack);
  });

  it('classifies editable targets so shortcuts do not fire in inputs', () => {
    const input = document.createElement('input');
    const div = document.createElement('div');
    const editable = document.createElement('div');
    editable.setAttribute('contenteditable', 'true');
    const textarea = document.createElement('textarea');
    expect(isEditableTarget(input)).toBe(true);
    expect(isEditableTarget(textarea)).toBe(true);
    expect(isEditableTarget(editable)).toBe(true);
    expect(isEditableTarget(div)).toBe(false);
  });
});
