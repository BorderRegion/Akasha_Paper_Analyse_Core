/**
 * UX-043 — entity identity and provenance come from stored records, not guesses.
 *
 * Requirement (docs/03 §S08): 方法与技巧不同实体，author/venue/dataset可由小标签打开
 * 实体抽屉。作者只显示可核对身份、机构、研究主题与合作关系，不按姓名推断能力或可靠性.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/app/App';
import { EntityDrawer } from '../../src/features/entities/EntityDrawer';
import { Providers } from '../../src/app/providers';
import { QueryClient } from '@tanstack/react-query';
import { ApiClient } from '../../src/api/client';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';

const AUTHOR = {
  entity_id: 'ent_author',
  entity_type: 'AUTHOR',
  name: 'A. Researcher',
  aliases: ['A. R.'],
  description: { value: null, missing_reason: 'NOT_EXTRACTED', source_refs: [] },
  relations: [
    {
      relation_type: 'AFFILIATED_WITH',
      direction: 'OUT',
      other_entity_id: 'ent_org',
      confidence: null,
    },
  ],
  source_refs: [
    { paper_id: 'pap_1', paper_version_id: null, claim_id: null, evidence_ids: [] },
    { paper_id: 'pap_2', paper_version_id: null, claim_id: null, evidence_ids: [] },
  ],
};

function renderDrawer(entityId = 'ent_author') {
  const server = createFakeServer({ entities: [AUTHOR] });
  const client = new ApiClient({ mode: 'TEST', fetchImpl: server.fetch });
  const result = render(
    <Providers client={client} queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <EntityDrawer entityId={entityId} onClose={() => undefined} />
    </Providers>,
  );
  return { server, ...result };
}

describe('UX-043 entity identity is never guessed', () => {
  it('shows the stored name, aliases, relations and source papers', async () => {
    renderDrawer();
    const drawer = await screen.findByTestId('entity-drawer');
    expect(await screen.findByText('A. Researcher')).toBeInTheDocument();
    expect(drawer).toHaveTextContent('A. R.');
    expect(drawer).toHaveTextContent('AFFILIATED_WITH');
    expect(drawer).toHaveTextContent('ent_org');
    const sources = await screen.findByTestId('entity-sources');
    expect(sources).toHaveTextContent('pap_1');
    expect(sources).toHaveTextContent('pap_2');
  });

  it('states an absent description as a missing reason instead of inventing one', async () => {
    renderDrawer();
    expect(await screen.findByText(/未报告（未提取）/)).toBeInTheDocument();
  });

  it('marks an unreported confidence instead of implying certainty', async () => {
    renderDrawer();
    expect(await screen.findByText(/置信度未报告/)).toBeInTheDocument();
  });

  it('never claims ability or reliability from a name', async () => {
    renderDrawer();
    const drawer = await screen.findByTestId('entity-drawer');
    await screen.findByText('A. Researcher');
    expect(drawer).toHaveTextContent('不按姓名推断能力或可靠性');
    expect(drawer.textContent ?? '').not.toMatch(/权威|顶级|影响力|可靠度\s*\d/i);
  });

  it('says so when an entity has no verifiable source', async () => {
    const server = createFakeServer({
      entities: [{ ...AUTHOR, source_refs: [], aliases: [], relations: [] }],
    });
    const client = new ApiClient({ mode: 'TEST', fetchImpl: server.fetch });
    render(
      <Providers client={client} queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <EntityDrawer entityId="ent_author" onClose={() => undefined} />
      </Providers>,
    );
    expect(await screen.findByText('没有可核对来源')).toBeInTheDocument();
    expect(screen.getByText('没有已记录的别名。')).toBeInTheDocument();
  });

  it('is reachable from the techniques page as a small tag, not as a full page', async () => {
    const server = createFakeServer({ entities: [AUTHOR] });
    renderApp(server, { path: '/app/techniques' });
    expect(await screen.findByTestId('technique-cards')).toBeInTheDocument();
    expect(screen.queryByTestId('entity-drawer')).not.toBeInTheDocument();
  });

  it('does not fetch an entity when none is selected', () => {
    const server = createFakeServer({ entities: [AUTHOR] });
    const client = new ApiClient({ mode: 'TEST', fetchImpl: server.fetch });
    render(
      <Providers client={client} queryClient={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <EntityDrawer entityId={null} onClose={() => undefined} />
      </Providers>,
    );
    expect(server.requests.filter((request) => request.path.startsWith('/v1/ui/entities/'))).toHaveLength(0);
    expect(App).toBeTruthy();
  });
});
