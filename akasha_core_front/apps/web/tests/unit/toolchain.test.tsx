import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { DEFAULT_PREFERENCES } from '../../src/state/theme';
import { createFakeServer } from '../support/fake-server';

describe('toolchain baseline (F00/UX-006)', () => {
  it('mounts the app shell with navigation and status line', async () => {
    render(<App mode="TEST" fetchImpl={createFakeServer().fetch} initialPreferences={DEFAULT_PREFERENCES} />);
    expect(await screen.findByRole('navigation', { name: '主导航' })).toBeInTheDocument();
    // The always-on status line is the footer landmark; page-level states also
    // use role=status, so query the landmark instead of a bare role.
    expect(screen.getByRole('contentinfo')).toHaveTextContent('Akasha · 留一点时间，给阅读。');
    expect(screen.getByRole('search')).toBeInTheDocument();
  });
});
