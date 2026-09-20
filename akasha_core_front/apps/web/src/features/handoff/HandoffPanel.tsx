/**
 * HandoffPanel — external-agent handoff preview (docs/03 §S12, docs/06 §审查与外部Agent).
 *
 * The preview ALWAYS shows what will be included, which scope it covers and the
 * size limit BEFORE anything is generated. PDF binaries and raw model output are
 * opt-in and off by default; the export is created by the server, and a bundle
 * that cannot fit is refused with the real numbers instead of being silently
 * truncated.
 */

import { useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import { formatBytes } from '../../lib/format';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import type { HandoffManifest } from '../../api/ui';
import styles from './handoff.module.css';

export interface HandoffPanelProps {
  workspaceId: string;
  /** Explicit scope: the versions currently in view (never "the whole library"). */
  scope: string[];
  defaultOpen?: boolean;
}

const INCLUDES: Array<{ id: string; label: string; note: string; optIn: boolean }> = [
  { id: 'brief', label: '摘要与版本', note: '题目、版本、分析 run', optIn: false },
  { id: 'claims', label: '结论', note: '逐字结论与其 support_state', optIn: false },
  { id: 'evidence_refs', label: '证据引用', note: 'claim→evidence 链接', optIn: false },
  { id: 'audit', label: '审计记录', note: '验证器结论与原因', optIn: false },
  { id: 'notes', label: '个人笔记', note: '你的笔记（私有）', optIn: false },
  { id: 'pdf', label: 'PDF 原文', note: '默认不带；仅按引用导出', optIn: true },
  { id: 'model_raw', label: '模型原始输出', note: '默认不带', optIn: true },
];

export function HandoffPanel({
  workspaceId,
  scope,
  defaultOpen = false,
}: HandoffPanelProps): ReactElement {
  const { api } = useSession();
  const [open, setOpen] = useState(defaultOpen);
  const [include, setInclude] = useState<string[]>(['brief', 'claims', 'evidence_refs']);
  const [maxBytes, setMaxBytes] = useState(2 * 1024 * 1024);
  const [result, setResult] = useState<HandoffManifest | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const uniqueScope = [...new Set(scope)].filter(Boolean);

  const create = async () => {
    setBusy(true);
    setErrorCode(null);
    setNote(null);
    try {
      const response = await api.createHandoff({
        paper_version_ids: uniqueScope,
        include,
        max_bytes: maxBytes,
      });
      setResult(response.data);
    } catch (error) {
      setErrorCode(errorCodeOf(error));
    } finally {
      setBusy(false);
    }
  };

  const copySummary = async () => {
    const summary = [
      `scope: ${uniqueScope.length} 个明确版本`,
      `include: ${include.join(', ')}`,
      'source hashes: 见 manifest（每篇一条 document_sha256）',
      `生成方: Akasha（workspace ${workspaceId}）`,
    ].join('\n');
    try {
      await navigator.clipboard.writeText(summary);
      setNote('小摘要已复制（仅文本，不含原文与模型原始输出）');
    } catch {
      setNote('浏览器拒绝了剪贴板访问，请在预览中手动复制');
    }
  };

  return (
    <section className={styles.panel} aria-labelledby="handoff-heading" data-testid="handoff-panel">
      <header className={styles.header}>
        <h2 id="handoff-heading">交给 Agent（交接包）</h2>
        <Button variant="quiet" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
          {open ? '收起' : '预览交接内容'}
        </Button>
      </header>

      {open ? (
        <div className={styles.body}>
          <p className="muted" data-testid="handoff-scope">
            范围：{uniqueScope.length} 个明确版本（不含全库，也不隐式扩大）
          </p>
          <ul className={styles.includeList}>
            {INCLUDES.map((entry) => (
              <li key={entry.id}>
                <label>
                  <input
                    type="checkbox"
                    checked={include.includes(entry.id)}
                    onChange={(event) =>
                      setInclude((current) =>
                        event.target.checked
                          ? [...current, entry.id]
                          : current.filter((item) => item !== entry.id),
                      )
                    }
                  />
                  {entry.label}
                  {entry.optIn ? <span className="muted">（需显式勾选）</span> : null}
                </label>
                <span className="muted">{entry.note}</span>
              </li>
            ))}
          </ul>

          <label>
            体积上限
            <input
              type="number"
              value={maxBytes}
              min={1024}
              step={1024}
              aria-label="体积上限（字节）"
              onChange={(event) => setMaxBytes(Number(event.target.value) || 1024)}
            />
            <span className="muted">{formatBytes(maxBytes)}</span>
          </label>

          <div className={styles.actions}>
            <Button onClick={() => void create()} disabled={busy || !uniqueScope.length}>
              {busy ? '生成中…' : '生成交接包'}
            </Button>
            <Button variant="quiet" onClick={() => void copySummary()}>
              复制小摘要
            </Button>
            {!uniqueScope.length ? (
              <span className="muted">先在左侧选择至少一项；交接包不会默认覆盖全库。</span>
            ) : null}
          </div>

          {result ? (
            <div data-testid="handoff-result">
              <p>
                已生成 {result.asset_id}（{formatBytes(result.size_bytes)}）：manifest 含
                schema_version、生成时间、范围与每篇的 source_hashes。
              </p>
              <p className="muted">
                包含内容：
                {String((result.manifest.include as string[] | undefined)?.join(', ') ?? '')}
              </p>
              <a
                href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(result.manifest, null, 2))}`}
                download={`${result.asset_id}.json`}
              >下载交接包 JSON</a>
              <ul>
                {(result.manifest.papers as Array<Record<string, unknown>> | undefined)?.map(
                  (paper) => (
                    <li key={String(paper.paper_version_id)}>
                      {String(paper.paper_version_id)} ·{' '}
                      {String(
                        (paper.source_hashes as Record<string, string> | undefined)
                          ?.document_sha256 ?? '未报告',
                      ).slice(0, 12)}
                      …
                    </li>
                  ),
                )}
              </ul>
            </div>
          ) : null}
          {errorCode ? (
            <p role="alert" data-testid="handoff-error">
              交接包未生成（{errorCode}）：体积或范围超出限制，未产生部分文件。
            </p>
          ) : null}
          {note ? (
            <p className="muted" data-testid="handoff-note">
              {note}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
