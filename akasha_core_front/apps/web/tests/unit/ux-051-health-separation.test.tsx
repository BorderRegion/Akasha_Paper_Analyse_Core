/**
 * UX-051 — the three health states stay separate.
 *
 * Requirement (docs/09 §三种健康): FrontendHealth / BackendHealth / ResearchQuality
 * 三者不合成一个万能绿色灯。例："阅读器正常 / OCR未连接 / 该结论尚未核查"可同时成立；
 * 模块已注册=配置状态，不是HEALTHY.
 */

import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createFakeServer } from '../support/fake-server';
import { renderApp } from '../support/render';
import {
  HEALTH_LABELS,
  backendHealth,
  clearClientEvents,
  frontendHealth,
  recordEvent,
  researchQualityHealth,
} from '../../src/state/clientEvents';

describe('UX-051 frontend / backend / research health are never merged', () => {
  it('computes each health from its own facts', () => {
    clearClientEvents();
    const frontend = frontendHealth({
      lastRequestStatus: 200,
      lastRequestAt: new Date().toISOString(),
      schemaIssueCount: 0,
      pdfEngineError: null,
      capabilitiesLoaded: true,
    });
    const backend = backendHealth({
      providerUnknownCount: 1,
      workerUnobserved: true,
      queueFailed: 0,
      diskUnmeasurable: false,
    });
    const research = researchQualityHealth({
      disputed: 2,
      unsupported: 1,
      notVerified: 5,
      staleVersions: 0,
    });

    // "Reader fine / OCR not connected / this claim not verified" coexist.
    expect(frontend.state).toBe('OK');
    expect(backend.state).toBe('UNKNOWN');
    expect(research.state).toBe('UNKNOWN');
    expect(new Set([frontend.state, backend.state, research.state]).size).toBeGreaterThan(1);
  });

  it('marks the frontend degraded when contract validation fails, without touching the others', () => {
    const frontend = frontendHealth({
      lastRequestStatus: 200,
      lastRequestAt: new Date().toISOString(),
      schemaIssueCount: 3,
      pdfEngineError: null,
      capabilitiesLoaded: true,
    });
    const backend = backendHealth({
      providerUnknownCount: 0,
      workerUnobserved: false,
      queueFailed: 0,
      diskUnmeasurable: false,
    });
    expect(frontend.state).toBe('DEGRADED');
    expect(frontend.reasons.join()).toContain('契约校验');
    expect(backend.state).toBe('OK');
  });

  it('reports a 5xx as frontend DOWN while the backend health stays independent', () => {
    const frontend = frontendHealth({
      lastRequestStatus: 503,
      lastRequestAt: new Date().toISOString(),
      schemaIssueCount: 0,
      pdfEngineError: null,
      capabilitiesLoaded: true,
    });
    expect(frontend.state).toBe('DOWN');
    expect(frontend.reasons.join()).toContain('503');
  });

  it('keeps research quality UNKNOWN when nothing was verified', () => {
    const research = researchQualityHealth({
      disputed: 0,
      unsupported: 0,
      notVerified: 4,
      staleVersions: 0,
    });
    // Zero disputes are NOT "everything verified": the audit simply returned
    // nothing for these claims.
    expect(research.state).toBe('UNKNOWN');
    expect(research.reasons.join()).toContain('尚未核查');
  });

  it('shows the three states separately in settings', async () => {
    const server = createFakeServer({
      snapshot: {
        queue: { pending: 0, running: 0, failed: 0 },
        workers: [{ identity: '(none)', last_seen: null, state: 'UNOBSERVED' }],
        modules: [{ module_id: 'agents.builtin', health: 'UNKNOWN', observed_at: null, source: 'manifest', reason: 'no probe' }],
        providers: [
          {
            id: 'prv_1',
            configured_limit: 40,
            observed_active: { value: null, unit: 'requests', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
            latency_p50: { value: null, unit: 'ms', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
            latency_p90: { value: null, unit: 'ms', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
            error_rate: { value: null, unit: 'ratio', state: 'UNKNOWN', observed_at: null, sample_count: 0, window_seconds: 300, reason: 'NOT_MEASURED' },
          },
        ],
        disk: [],
        eta_available: false,
        observed_at: new Date().toISOString(),
        unknown_reasons: [],
      },
      systemStatus: { modules: [{ module_id: 'agents.builtin' }], providers: {} },
    });
    renderApp(server, { path: '/app/settings' });

    const split = await screen.findByTestId('health-split');
    expect(split).toHaveTextContent('前端：');
    expect(split).toHaveTextContent('后端：');
    expect(split).toHaveTextContent('研究质量：');
    // The backend row explains WHY it is not "healthy".
    expect(split).toHaveTextContent('worker 心跳');
    expect(split.textContent ?? '').not.toMatch(/全部正常|一切正常|all good/i);
    expect(HEALTH_LABELS.UNKNOWN).toBe('未观测');
  });

  it('never shows a single green light for the whole system', async () => {
    clearClientEvents();
    recordEvent({ kind: 'schema', detail: 'contract mismatch', errorCode: 'SCHEMA_001' });
    const server = createFakeServer({});
    renderApp(server, { path: '/app/settings' });
    const split = await screen.findByTestId('health-split');
    const items = split.querySelectorAll('li');
    expect(items).toHaveLength(3);
    for (const item of items) {
      expect(item.textContent).toMatch(/前端：|后端：|研究质量：/);
    }
  });
});
