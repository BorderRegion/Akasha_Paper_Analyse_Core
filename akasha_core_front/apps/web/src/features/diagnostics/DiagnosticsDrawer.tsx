/**
 * DiagnosticsDrawer (docs/09 §UI Inspector).
 *
 * Read-only, opened from Settings or Ctrl/Cmd+Shift+D. It shows the route, UI
 * build hash, contract version, the active paper/version/claim, capabilities, the
 * last 20 requests, the last schema errors, the current component state, client
 * request / trace ids and the last successful snapshot time.
 *
 * The copied report contains NO request bodies, notes, search text, tokens,
 * authorization headers or cookies. An evidence excerpt is a separate opt-in with
 * a preview, and the report stays under the 2 MiB server cap.
 */

import { useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import { Modal } from '../../components/ui/Modal';
import { schemaIssues } from '../../api/validate';
import { clientEvents, MAX_CLIENT_EVENTS } from '../../state/clientEvents';
import { useSession } from '../../state/session';
import styles from './diagnostics.module.css';

export interface DiagnosticsDrawerProps {
  open: boolean;
  onClose(): void;
  route: string;
  activePaper?: { paperId: string; paperVersionId: string | null; claimId: string | null };
  componentState?: string;
  lastSnapshotAt?: string | null;
  buildHash: string;
  evidenceExcerpt?: string | null;
}

export function DiagnosticsDrawer({
  open,
  onClose,
  route,
  activePaper,
  componentState = 'ready',
  lastSnapshotAt = null,
  buildHash,
  evidenceExcerpt = null,
}: DiagnosticsDrawerProps): ReactElement | null {
  const { capabilities, bootstrap, api } = useSession();
  const [includeExcerpt, setIncludeExcerpt] = useState(false);
  const [report, setReport] = useState<{ assetId: string; sizeBytes: number; maxBytes: number } | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copyNotice, setCopyNotice] = useState<string | null>(null);

  if (!open) return null;

  const events = clientEvents();
  const requests = events.filter((event) => event.kind === 'request').slice(-20);
  const issues = schemaIssues();

  const copyReport = async () => {
    const payload = JSON.stringify(
      {
        route,
        ui_build: buildHash,
        ui_contract_version: bootstrap?.ui_contract_version ?? 'unknown',
        active_paper: activePaper ?? null,
        capabilities: capabilities?.capabilities.map((entry) => entry.code) ?? [],
        recent_requests: requests,
        schema_errors: issues,
        component_state: componentState,
        last_successful_snapshot_at: lastSnapshotAt,
        events_kept: events.length,
      },
      null,
      2,
    );
    try { await navigator.clipboard.writeText(payload); setCopyNotice('已复制脱敏报告。'); }
    catch { setCopyNotice('复制失败，请检查浏览器的剪贴板权限。'); }
  };

  return (
    <Modal open={open} onClose={onClose} title="诊断（只读）" className={styles.drawer} aria-label="诊断抽屉" data-testid="diagnostics-drawer">

      <dl className={styles.facts}>
        <dt>route</dt>
        <dd data-testid="diag-route">{route}</dd>
        <dt>UI build</dt>
        <dd>{buildHash}</dd>
        <dt>ui_contract_version</dt>
        <dd>{bootstrap?.ui_contract_version ?? '未知'}</dd>
        <dt>当前论文 / 版本 / claim</dt>
        <dd>
          {activePaper
            ? `${activePaper.paperId} · ${activePaper.paperVersionId ?? '未选版本'} · ${
                activePaper.claimId ?? '未选 claim'
              }`
            : '未选择'}
        </dd>
        <dt>component state</dt>
        <dd>{componentState}</dd>
        <dt>最后成功 snapshot</dt>
        <dd>{lastSnapshotAt ?? '未报告'}</dd>
        <dt>客户端事件</dt>
        <dd data-testid="diag-events">
          保留 {events.length}/{MAX_CLIENT_EVENTS} 条（会话内存）
        </dd>
      </dl>

      <h3>最后 20 次请求</h3>
      <table className={styles.table} tabIndex={0} aria-label="最近请求，可横向滚动">
        <thead>
          <tr>
            <th scope="col">方法</th>
            <th scope="col">路由模板</th>
            <th scope="col">状态</th>
            <th scope="col">耗时</th>
            <th scope="col">trace</th>
          </tr>
        </thead>
        <tbody>
          {requests.map((event, index) => (
            <tr key={`${event.at}-${index}`}>
              <td>{event.method}</td>
              <td>{event.routeTemplate}</td>
              <td>{event.status}</td>
              <td>{event.durationMs}ms</td>
              <td>{event.traceId ?? '—'}</td>
            </tr>
          ))}
          {!requests.length ? (
            <tr>
              <td colSpan={5} className="muted">
                还没有请求记录。
              </td>
            </tr>
          ) : null}
        </tbody>
      </table>

      <h3>契约校验问题</h3>
      {issues.length ? (
        <ul>
          {issues.slice(0, 10).map((issue, index) => (
            <li key={`${issue.path}-${index}`}>
              {issue.path}: 期望 {issue.expected}，实际 {issue.actual}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">没有记录到契约问题。</p>
      )}

      <h3>导出诊断</h3>
      <p className="muted">
        报告默认不含请求正文、笔记、搜索原文、token、authorization 与 cookie；上限 2 MiB。
      </p>
      <label>
        <input
          type="checkbox"
          checked={includeExcerpt}
          onChange={(event) => setIncludeExcerpt(event.target.checked)}
        />
        包含当前证据摘录（分享前请确认）
      </label>
      {includeExcerpt && evidenceExcerpt ? (
        <pre className={styles.preview} data-testid="excerpt-preview">
          {evidenceExcerpt.slice(0, 400)}
        </pre>
      ) : null}

      <div className={styles.actions}>
        <Button variant="quiet" onClick={() => void copyReport()}>
          复制脱敏报告
        </Button>
        <Button
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setErrorCode(null);
            try {
              const response = await api.diagnostics({
                  include_evidence_excerpt: includeExcerpt,
                  client: {
                    route,
                    build: buildHash,
                    component_state: componentState,
                    last_snapshot_at: lastSnapshotAt,
                  },
                  events,
                  evidence_excerpt: includeExcerpt ? evidenceExcerpt : null,
              });
              setReport({
                assetId: response.data.asset_id,
                sizeBytes: response.data.size_bytes,
                maxBytes: response.data.max_bytes ?? 2 * 1024 * 1024,
              });
            } catch {
              setErrorCode('INTERNAL_001');
            } finally {
              setBusy(false);
            }
          }}
        >
          生成诊断包（服务端）
        </Button>
      </div>
      {copyNotice ? <p role="status">{copyNotice}</p> : null}
      {report ? (
        <p role="status" data-testid="diag-report">
          诊断包 {report.assetId}（{report.sizeBytes} / {report.maxBytes} 字节）
        </p>
      ) : null}
      {errorCode ? <p role="alert">诊断包生成失败（{errorCode}）。</p> : null}
    </Modal>
  );
}
