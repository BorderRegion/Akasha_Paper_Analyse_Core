/**
 * SettingsPage (docs/03 §S11 + docs/09).
 *
 * Five sections: appearance, connection, storage, export, diagnostics. It never
 * pretends a provider key can be edited in the browser (keys live on the server),
 * and it shows the THREE separate health states instead of one green light.
 */

import { useState, type ReactElement } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Button } from '../../components/ui/Button';
import { DiagnosticsDrawer } from '../diagnostics/DiagnosticsDrawer';
import {
  HEALTH_LABELS,
  backendHealth,
  clientEvents,
  frontendHealth,
} from '../../state/clientEvents';
import { usePreferences } from '../../state/preferences';
import type { Preferences } from '../../state/theme';
import { errorCodeOf } from '../../lib/apiError';
import { schemaIssues } from '../../api/validate';
import { useSession } from '../../state/session';
import styles from './settings.module.css';

export interface SettingsPageProps {
  buildHash?: string;
}

export function SettingsPage({ buildHash = 'dev' }: SettingsPageProps): ReactElement {
  const { preferences, update } = usePreferences();
  const { client, capabilities, bootstrap, status, logout } = useSession();
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const snapshot = useQuery({
    queryKey: ['ui', 'operations-snapshot'] as const,
    queryFn: async () => (await client.request<Record<string, never>>('/v1/ui/operations/snapshot')).data,
  });
  const systemStatus = useQuery({
    queryKey: ['core', 'system-status'] as const,
    queryFn: async () =>
      (await client.request<{ modules?: unknown[]; providers?: unknown[] }>('/v1/ui/system/status')).data,
  });

  const frontend = frontendHealth({
    lastRequestStatus: clientEvents().filter((event) => event.kind === 'request').at(-1)?.status ?? null,
    lastRequestAt: clientEvents().filter((event) => event.kind === 'request').at(-1)?.at ?? null,
    schemaIssueCount: schemaIssues().length,
    pdfEngineError: null,
    capabilitiesLoaded: Boolean(capabilities),
  });
  const providers = (snapshot.data as { providers?: { observed_active: { state: string } }[] } | undefined)?.providers ?? [];
  const workers = (snapshot.data as { workers?: { state: string }[] } | undefined)?.workers ?? [];
  const disk =
    (snapshot.data as { disk?: { mount_id?: string; measurable?: boolean }[] } | undefined)?.disk ?? [];
  const backend = backendHealth({
    providerUnknownCount: providers.filter((provider) => provider.observed_active.state === 'UNKNOWN').length,
    workerUnobserved: !workers.length || workers.every((worker) => worker.state === 'UNOBSERVED'),
    queueFailed: (snapshot.data as { queue?: { failed?: number } } | undefined)?.queue?.failed ?? 0,
    diskUnmeasurable: disk.some((mount) => mount.measurable === false),
  });
  const research = {
    state: 'UNKNOWN' as const,
    reasons: ['尚未取得全库研究质量统计，请在复核页查看具体结论'],
  };

  return (
    <section className={styles.page} aria-labelledby="settings-heading">
      <h1 id="settings-heading">设置</h1>
      <p className="muted">调成你习惯的样子。</p>

      <section className={styles.block} aria-labelledby="appearance-heading">
        <h2 id="appearance-heading">外观</h2>
        <label>
          主题
          <select
            aria-label="主题"
            value={preferences.theme}
            onChange={(event) => update({ theme: event.target.value as Preferences['theme'] })}
          >
            <option value="light">浅色</option>
            <option value="dark">深色</option>
            <option value="system">跟随系统</option>
          </select>
        </label>
        <label>
          密度
          <select
            aria-label="密度"
            value={preferences.density}
            onChange={(event) => update({ density: event.target.value as Preferences['density'] })}
          >
            <option value="comfortable">舒适</option>
            <option value="compact">紧凑</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={preferences.reduce_motion}
            onChange={(event) => update({ reduce_motion: event.target.checked })}
          />
          减少动效
        </label>
        <label>
          <input
            type="checkbox"
            checked={preferences.focus_default}
            onChange={(event) => update({ focus_default: event.target.checked })}
          />
          默认专注模式
        </label>
      </section>

      <section className={styles.block} aria-labelledby="connection-heading">
        <h2 id="connection-heading">连接</h2>
        <p className="muted" data-testid="connection-state">
          服务器状态：{status === 'authenticated' ? '已连接' : status === 'anonymous' ? '需要登录' : status}
          · {bootstrap?.auth_required ? '已启用登录保护' : '本机无需登录'}
        </p>
        <p className="muted">
          模型与 API 密钥在服务器上配置，不会保存在浏览器里。
        </p>
        {bootstrap?.auth_required && status === 'authenticated' ? (
          <Button onClick={() => void logout().catch(() => {
            setTestError('退出请求未完成，请重试或关闭浏览器。');
          })}>退出登录</Button>
        ) : null}
        <Button
          disabled={testing}
          onClick={async () => {
            setTesting(true);
            setTestError(null);
            setTestResult(null);
            try {
              await client.request('/v1/ui/bootstrap');
              await client.request('/v1/ui/capabilities');
              setTestResult('连接正常，登录与功能检查已通过。');
            } catch (error) {
              const code = errorCodeOf(error);
              // A database failure and an authentication failure are different
              // problems and must read differently.
              setTestError(code === 'AUTH_001' ? 'AUTH_001：登录已失效，请重新登录。' : `${code ?? '未知错误'}`);
            } finally {
              setTesting(false);
            }
          }}
        >
          {testing ? '测试中…' : '测试连接'}
        </Button>
        {testResult ? <p role="status" data-testid="test-ok">{testResult}</p> : null}
        {testError ? <p role="alert" data-testid="test-error">{testError}</p> : null}
        <h3>各项状态</h3>
        <ul className={styles.health} data-testid="health-split">
          <li>
            前端：{HEALTH_LABELS[frontend.state]}
            <span className="muted">（{frontend.reasons.join('；') || '页面与数据加载正常'}）</span>
          </li>
          <li>
            后端：{HEALTH_LABELS[backend.state]}
            <span className="muted">（{backend.reasons.join('；') || '模块探针与队列正常'}）</span>
          </li>
          <li>
            研究质量：{HEALTH_LABELS[research.state]}
            <span className="muted">（{research.reasons.join('；') || '未发现问题'}）</span>
          </li>
        </ul>
      </section>

      <section className={styles.block} aria-labelledby="storage-heading">
        <h2 id="storage-heading">存储</h2>
        <ul data-testid="storage-mounts">
          {disk.map((mount, index) => (
            <li key={`${mount.mount_id ?? index}`}>
              {mount.mount_id}: {mount.measurable === false ? '不可测量' : '可测量'}
            </li>
          ))}
          {!disk.length ? <li className="muted">尚未读取磁盘观测。</li> : null}
        </ul>
        <p className="muted">可在「运行状态」预览并清理临时文件。常规清理不会删除 PDF、证据或笔记。</p>
      </section>

      <section className={styles.block} aria-labelledby="export-heading">
        <h2 id="export-heading">导出</h2>
        <p className="muted">
          证据包和笔记通过「交给 Agent」导出；外观偏好保存在当前浏览器。
        </p>
        <p className="muted">
          已配置 {(systemStatus.data?.modules as unknown[] | undefined)?.length ?? 0} 个模块，运行情况可在「运行状态」查看。
        </p>
      </section>

      <section className={styles.block} aria-labelledby="diagnostics-heading">
        <h2 id="diagnostics-heading">诊断</h2>
        <Button onClick={() => setDiagnosticsOpen(true)}>打开诊断抽屉</Button>
      </section>

      <DiagnosticsDrawer
        open={diagnosticsOpen}
        onClose={() => setDiagnosticsOpen(false)}
        route={typeof window === 'undefined' ? '/' : window.location.pathname}
        componentState={status}
        lastSnapshotAt={(snapshot.data as { observed_at?: string } | undefined)?.observed_at ?? null}
        buildHash={buildHash}
      />
    </section>
  );
}
