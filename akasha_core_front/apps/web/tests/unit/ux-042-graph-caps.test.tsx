/**
 * UX-042 — the local relationship graph has node/edge caps and a list alternative.
 *
 * Requirement (docs/03 §S07): 关系图默认局部≤40节点、≤80边，布局生成后稳定；有同等列表
 * 替代；双击节点只展开一跳，上限显式提示，不强制动画仿真.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import {
  MAX_EDGES,
  MAX_NODES,
  RelationshipView,
  layout,
  sliceGraph,
} from '../../src/features/graph/RelationshipView';

function nodes(count: number) {
  return Array.from({ length: count }, (_, index) => ({ id: `n${index}`, label: `节点 ${index}` }));
}

describe('UX-042 relationship graph caps and list alternative', () => {
  it('caps nodes at 40 and edges at 80', () => {
    const many = nodes(60);
    const edges = Array.from({ length: 200 }, (_, index) => ({
      source: `n${index % 60}`,
      target: `n${(index + 1) % 60}`,
    }));
    const slice = sliceGraph(many, edges);
    expect(slice.nodes).toHaveLength(MAX_NODES);
    expect(slice.edges).toHaveLength(MAX_EDGES);
    expect(slice.truncatedNodes).toBe(20);
    expect(slice.truncatedEdges).toBe(120);
  });

  it('states the cap and the truncation explicitly', () => {
    render(
      <RelationshipView
        nodes={nodes(50)}
        edges={[{ source: 'n0', target: 'n1' }]}
      />,
    );
    const caps = screen.getByTestId('graph-caps');
    expect(caps).toHaveTextContent('节点上限 40');
    expect(caps).toHaveTextContent('边上限 80');
    expect(caps).toHaveTextContent('已截断 10 个节点');
  });

  it('never draws an edge whose endpoints were dropped', () => {
    const slice = sliceGraph(nodes(3), [
      { source: 'n0', target: 'n2' },
      { source: 'n0', target: 'n99' },
    ]);
    expect(slice.edges).toHaveLength(1);
    expect(slice.edges[0]?.target).toBe('n2');
  });

  it('offers an equivalent list that shows every node, even truncated ones', async () => {
    const user = userEvent.setup();
    render(<RelationshipView nodes={nodes(50)} edges={[{ source: 'n0', target: 'n1' }]} />);

    await user.click(screen.getByRole('button', { name: /用列表查看/ }));
    const list = screen.getByTestId('relationship-list');
    expect(list.querySelectorAll(':scope > li')).toHaveLength(50);
    expect(list).toHaveTextContent('节点 49');
    expect(screen.queryByTestId('graph-svg')).not.toBeInTheDocument();
  });

  it('keeps the layout stable for the same nodes', () => {
    const first = layout(nodes(5));
    const second = layout(nodes(5));
    expect(second).toEqual(first);
    expect(Object.keys(first)).toHaveLength(5);
  });

  it('expands exactly one hop on double click', async () => {
    const onExpand = vi.fn();
    const user = userEvent.setup();
    render(<RelationshipView nodes={nodes(3)} edges={[]} onExpand={onExpand} />);

    await user.dblClick(screen.getByTestId('graph-svg').querySelector('[data-node-id="n1"]') as Element);
    expect(onExpand).toHaveBeenCalledTimes(1);
    expect(onExpand).toHaveBeenCalledWith('n1');
  });

  it('labels arrows as dependencies, not progress', () => {
    render(<RelationshipView nodes={nodes(2)} edges={[{ source: 'n0', target: 'n1' }]} />);
    expect(screen.getByText(/箭头表示依赖关系，不表示完成百分比/)).toBeInTheDocument();
  });
});
