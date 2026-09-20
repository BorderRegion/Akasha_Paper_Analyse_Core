/**
 * RelationshipView (docs/03 §S07).
 *
 * The graph is LOCAL by design: at most 40 nodes and 80 edges, with the cap
 * stated explicitly, and an EQUIVALENT LIST is always available (the graph is
 * never the only way to read the data). Layout is computed once and stays stable;
 * there is no animated simulation, and expanding a node opens one hop only.
 */

import { useMemo, useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import styles from './graph.module.css';

export const MAX_NODES = 40;
export const MAX_EDGES = 80;

export interface GraphNode {
  id: string;
  label: string;
}

export interface GraphEdge {
  source: string;
  target: string;
  label?: string;
}

export interface RelationshipViewProps {
  nodes: GraphNode[];
  edges: GraphEdge[];
  /** Called when the user expands one hop from a node. */
  onExpand?(nodeId: string): void;
}

export interface GraphSlice {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncatedNodes: number;
  truncatedEdges: number;
}

/** Deterministic slice: caps are applied to the FIRST items, not at random. */
export function sliceGraph(nodes: GraphNode[], edges: GraphEdge[]): GraphSlice {
  const keptNodes = nodes.slice(0, MAX_NODES);
  const keptIds = new Set(keptNodes.map((node) => node.id));
  const keptEdges = edges
    .filter((edge) => keptIds.has(edge.source) && keptIds.has(edge.target))
    .slice(0, MAX_EDGES);
  return {
    nodes: keptNodes,
    edges: keptEdges,
    truncatedNodes: Math.max(0, nodes.length - keptNodes.length),
    truncatedEdges: Math.max(0, edges.length - keptEdges.length),
  };
}

/** A stable layout: nodes are placed on a circle in their given order. */
export function layout(nodes: GraphNode[]): Record<string, { x: number; y: number }> {
  const radius = 140;
  const positions: Record<string, { x: number; y: number }> = {};
  nodes.forEach((node, index) => {
    const angle = (2 * Math.PI * index) / Math.max(1, nodes.length);
    positions[node.id] = {
      x: 160 + radius * Math.cos(angle),
      y: 160 + radius * Math.sin(angle),
    };
  });
  return positions;
}

export function RelationshipView({ nodes, edges, onExpand }: RelationshipViewProps): ReactElement {
  const [asList, setAsList] = useState(false);
  const slice = useMemo(() => sliceGraph(nodes, edges), [nodes, edges]);
  const positions = useMemo(() => layout(slice.nodes), [slice.nodes]);

  return (
    <section className={styles.wrap} aria-labelledby="graph-heading" data-testid="relationship-view">
      <header className={styles.header}>
        <h2 id="graph-heading">关系（局部视图）</h2>
        <p className="muted" data-testid="graph-caps">
          节点上限 {MAX_NODES}、边上限 {MAX_EDGES}；当前显示 {slice.nodes.length} 个节点 /{' '}
          {slice.edges.length} 条边
          {slice.truncatedNodes || slice.truncatedEdges
            ? `（已截断 ${slice.truncatedNodes} 个节点、${slice.truncatedEdges} 条边，可用列表查看全部）`
            : ''}
        </p>
        <Button variant="quiet" onClick={() => setAsList((value) => !value)} aria-pressed={asList}>
          {asList ? '显示关系图' : '用列表查看（等价）'}
        </Button>
      </header>

      {asList ? (
        <ul className={styles.list} data-testid="relationship-list">
          {nodes.map((node) => (
            <li key={node.id}>
              <strong>{node.label}</strong>
              <ul>
                {edges
                  .filter((edge) => edge.source === node.id || edge.target === node.id)
                  .map((edge) => (
                    <li key={`${edge.source}-${edge.target}-${edge.label ?? ''}`}>
                      {edge.source === node.id ? '→' : '←'} {edge.label ?? 'relation'}{' '}
                      {edge.source === node.id ? edge.target : edge.source}
                    </li>
                  ))}
              </ul>
            </li>
          ))}
        </ul>
      ) : (
        <svg
          className={styles.canvas}
          viewBox="0 0 320 320"
          role="img"
          aria-label={`关系图：${slice.nodes.length} 个节点，${slice.edges.length} 条边`}
          data-testid="graph-svg"
        >
          {slice.edges.map((edge) => {
            const from = positions[edge.source];
            const to = positions[edge.target];
            if (!from || !to) return null;
            return (
              <line
                key={`${edge.source}-${edge.target}-${edge.label ?? ''}`}
                x1={from.x}
                y1={from.y}
                x2={to.x}
                y2={to.y}
                className={styles.edge}
              />
            );
          })}
          {slice.nodes.map((node) => (
            <g
              key={node.id}
              data-node-id={node.id}
              onDoubleClick={() => onExpand?.(node.id)}
              className={styles.node}
            >
              <circle cx={positions[node.id]?.x} cy={positions[node.id]?.y} r={10} />
              <text x={positions[node.id]?.x} y={(positions[node.id]?.y ?? 0) + 24} textAnchor="middle">
                {node.label}
              </text>
            </g>
          ))}
        </svg>
      )}
      <p className="muted">
        双击节点只展开一跳；箭头表示依赖关系，不表示完成百分比。
      </p>
    </section>
  );
}
