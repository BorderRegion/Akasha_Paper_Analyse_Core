import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { createFakeServer } from '../support/fake-server';

describe('production authentication entry', () => {
  it('gates private queries, logs in, restores a reload, and logs out', async () => {
    const server = createFakeServer({ authRequired: true });
    window.history.replaceState({}, '', '/app/settings');
    const user = userEvent.setup();
    const first = render(<App mode="TEST" fetchImpl={server.fetch} />);
    const input = await screen.findByLabelText('应用 token');
    expect(server.requests.some(r => r.path.startsWith('/v1/ui/library'))).toBe(false);
    await user.type(input, 'application-secret');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByTestId('connection-state');
    expect(JSON.stringify(localStorage)).not.toContain('application-secret');
    expect(JSON.stringify(sessionStorage)).not.toContain('application-secret');
    first.unmount();
    render(<App mode="TEST" fetchImpl={server.fetch} />);
    await screen.findByTestId('connection-state');
    await user.click(screen.getByRole('button', { name: '退出登录' }));
    await screen.findByLabelText('应用 token');
    expect(screen.queryByTestId('connection-state')).not.toBeInTheDocument();
    const deletion = server.requests.find(r => r.method === 'DELETE' && r.path === '/v1/ui/session');
    expect(deletion?.headers['X-CSRF-Token']).toBe('csrf-from-server');
  });

  it('keeps a rejected credential masked and allows editing and retry', async () => {
    const server = createFakeServer({ authRequired: true });
    let reject = true;
    const fetchImpl: typeof fetch = async (input, init) => {
      if (String(input).endsWith('/v1/ui/session') && init?.method === 'POST' && reject) {
        return new Response(JSON.stringify({ error: { code: 'AUTH_001', message: 'invalid token' } }), { status: 401 });
      }
      return server.fetch(input, init);
    };
    window.history.replaceState({}, '', '/app/settings');
    const user = userEvent.setup();
    render(<App mode="TEST" fetchImpl={fetchImpl} />);
    await user.type(await screen.findByLabelText('应用 token'), 'rejected-secret');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('应用 token')).toHaveValue('rejected-secret');
    expect(screen.getByLabelText('应用 token')).toHaveAttribute('type', 'password');
    expect(document.body.textContent).not.toContain('rejected-secret');
    reject = false;
    await user.clear(screen.getByLabelText('应用 token'));
    await user.type(screen.getByLabelText('应用 token'), 'valid-secret');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await waitFor(() => expect(screen.getByTestId('connection-state')).toHaveTextContent('已连接'));
  });

  it('trims pasted whitespace, allows revealing the token, and never persists it', async () => {
    const server = createFakeServer({ authRequired: true });
    let submitted: unknown;
    const fetchImpl: typeof fetch = async (input, init) => {
      if (String(input).endsWith('/v1/ui/session') && init?.method === 'POST') {
        submitted = JSON.parse(String(init.body));
      }
      return server.fetch(input, init);
    };
    window.history.replaceState({}, '', '/app/settings');
    render(<App mode="TEST" fetchImpl={fetchImpl} />);
    const user = userEvent.setup();
    const input = await screen.findByLabelText('应用 token');
    await user.type(input, '  pasted-secret  ');
    await user.click(screen.getByRole('button', { name: '显示' }));
    expect(input).toHaveAttribute('type', 'text');
    await user.click(screen.getByRole('button', { name: '隐藏' }));
    expect(input).toHaveAttribute('type', 'password');
    await user.click(screen.getByRole('button', { name: '登录' }));
    await screen.findByTestId('connection-state');
    expect(submitted).toEqual({ token: 'pasted-secret' });
    expect(JSON.stringify(localStorage) + JSON.stringify(sessionStorage)).not.toContain('pasted-secret');
  });
});
