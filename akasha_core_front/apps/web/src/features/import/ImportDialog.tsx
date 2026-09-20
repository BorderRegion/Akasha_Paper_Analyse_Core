/**
 * ImportDialog (docs/03 §S03).
 *
 * - drag or pick several PDFs; the limits come from `capabilities`, never from a
 *   constant in the UI;
 * - file states: 待上传 → 上传中 → 已接收 → 排队解析 → 完成/重复/失败, where
 *   "upload finished" is explicitly NOT "analysis finished";
 * - a duplicate offers "打开已有论文" instead of creating a copy;
 * - one failed file never clears the successful ones, and retry covers only the
 *   failed files with the SAME idempotency key;
 * - closing the dialog does not cancel already-received work.
 */

import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import { Button } from '../../components/ui/Button';
import { Modal } from '../../components/ui/Modal';
import { useQueryClient } from '@tanstack/react-query';
import { formatBytes } from '../../lib/format';
import { errorCodeOf } from '../../lib/apiError';
import { UploadQueue, type UploadRow } from '../../state/queries';
import { useSession } from '../../state/session';
import styles from './import.module.css';

export interface ImportDialogProps {
  open: boolean;
  onClose(): void;
}

const STATE_LABELS: Record<UploadRow['state'], string> = {
  PENDING: '待上传',
  UPLOADING: '上传中',
  RECEIVED: '已接收',
  QUEUED: '排队解析',
  IMPORTED: '已上传',
  DUPLICATE: '重复',
  FAILED: '失败',
  CANCELLED: '已取消',
};

const ERROR_LABELS: Record<string, string> = {
  PDF_001: '不是有效的 PDF 或文件已损坏',
  PDF_002: 'PDF 已加密或不受支持',
  STORAGE_001: '超出体积上限或服务器写入失败',
  RESOURCE_001: '服务器可用空间不足',
  CFG_002: '文件个数超出批次上限',
};

export function ImportDialog({ open, onClose }: ImportDialogProps): ReactElement | null {
  const { api, capabilities, client } = useSession();
  const queryClient = useQueryClient();
  const [rows, setRows] = useState<UploadRow[]>([]);
  const [batchId, setBatchId] = useState<string | null>(null);
  const [tier, setTier] = useState('T2_FULL');
  const [batchError, setBatchError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const queueRef = useRef<UploadQueue | null>(null);
  const batchPromise = useRef<Promise<string> | null>(null);
  const alive = useRef(true);
  const [creatingBatch, setCreatingBatch] = useState(false);
  const [requeueing, setRequeueing] = useState(false);
  const [retryNotice, setRetryNotice] = useState<string | null>(null);

  const limits = capabilities?.limits;

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; queueRef.current?.dispose(); };
  }, []);

  const ensureBatch = useCallback(async (): Promise<string> => {
    if (batchId) return batchId;
    if (batchPromise.current) return batchPromise.current;
    setCreatingBatch(true);
    batchPromise.current = api.createImportBatch({ requested_tier: tier }).then(response => {
      if (alive.current) setBatchId(response.data.batch_id);
      return response.data.batch_id;
    }).finally(() => { batchPromise.current = null; if (alive.current) setCreatingBatch(false); });
    return batchPromise.current;
  }, [api, batchId, tier]);

  const addFiles = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      try {
        const id = await ensureBatch();
        if (!alive.current) return;
        setBatchError(null);
        if (!queueRef.current) {
          queueRef.current = new UploadQueue({
            batchId: id,
            concurrency: 2,
            maxFileBytes: limits?.upload_file_bytes,
            maxFiles: limits?.upload_files,
            maxBatchBytes: limits?.upload_batch_bytes,
            onChange: setRows,
            upload: async (targetBatch, file, options) => {
              const response = await api.uploadFile(targetBatch, file, {
                idempotencyKey: options.idempotencyKey,
              });
              const data = response.data;
              void queryClient.invalidateQueries({ queryKey: ['ui', 'library'] });
              void queryClient.invalidateQueries({ queryKey: ['core', 'jobs'] });
              return {
                item_id: data.item_id,
                state: data.state,
                error_code: data.error_code,
                paper_id: data.paper_id,
                job_id: data.job_id,
              };
            },
          });
        }
        queueRef.current.enqueueFiles(files);
      } catch (error) {
        setBatchError(errorCodeOf(error) ?? 'INTERNAL_001');
      }
    },
    [api, ensureBatch, limits, queryClient],
  );

  const failed = useMemo(() => rows.filter((row) => row.state === 'FAILED'), [rows]);
  const succeeded = rows.filter((row) => row.state === 'IMPORTED' || row.state === 'DUPLICATE');
  const uploading = creatingBatch || rows.some(row => row.state === 'PENDING' || row.state === 'UPLOADING');

  if (!open) return null;

  return (
    <Modal open={open} onClose={onClose} title="导入 PDF"
      className={styles.dialog}
      onDragOver={(event) => {
        event.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        const files = Array.from(event.dataTransfer?.files ?? []);
        void addFiles(files);
      }}
      data-dragging={dragging ? 'true' : undefined}
    >
      <ol className={styles.workflow} aria-label="导入流程">
        <li><span aria-hidden="true">01</span>上传 PDF</li>
        <li><span aria-hidden="true">02</span>解析原文</li>
        <li><span aria-hidden="true">03</span>{tier === 'T0_INDEX' ? '整理索引' : '分析与核查'}</li>
      </ol>
      <details className="muted"><summary>文件大小与数量限制</summary><p>
        {limits
          ? `上限：单文件 ${formatBytes(limits.upload_file_bytes)}，每批 ${limits.upload_files} 个 / ${formatBytes(
              limits.upload_batch_bytes,
            )}；同时最多 2 个上传。`
          : '上限正在从服务器读取…'}
      </p></details>

      <label>
        分析深度
        <select aria-label="分析深度" value={tier} disabled={batchId !== null || creatingBatch}
          onChange={(event) => setTier(event.target.value)}>
          <option value="T0_INDEX">仅索引（不调用模型）</option>
          <option value="T1_SCAN">快速扫描</option>
          <option value="T2_FULL">完整分析</option>
          <option value="T3_DEEP">深度分析</option>
        </select>
      </label>
      {batchId ? <div className={styles.failed}>
        <span className="muted">本轮分析深度已确定。</span>
        <Button variant="quiet" disabled={uploading || requeueing} onClick={() => {
          queueRef.current?.dispose(); queueRef.current = null; setBatchId(null); setRows([]); setBatchError(null); setRetryNotice(null);
        }}>开始新一轮导入</Button>
      </div> : null}
      <label className={styles.picker}>
        <span>＋ 选择 PDF 文件</span>
        <span className="muted">也可以拖到这里 · 支持多选</span>
        <input
          className="srOnly"
          aria-label="选择文件"
          type="file"
          multiple
          accept="application/pdf"
          onChange={(event) => {
            void addFiles(Array.from(event.target.files ?? []));
            event.target.value = '';
          }}
        />
      </label>

      {batchError ? (
        <p role="alert">导入请求未完成（{batchError}），请重试。</p>
      ) : null}

      <p className="muted">上传后还需要解析和分析。收起此窗口不会停止上传；关闭浏览器前请等上传结束。</p>
      {rows.length ? <table className={styles.table}>
        <thead>
          <tr>
            <th scope="col">文件</th>
            <th scope="col">大小</th>
            <th scope="col">状态</th>
            <th scope="col">说明</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key} data-state={row.state} data-testid={`import-row-${row.state}`}>
              <td>{row.file.name}</td>
              <td>{formatBytes(row.file.size)}</td>
              <td>{STATE_LABELS[row.state]}</td>
              <td>
                {row.state === 'FAILED' && row.errorCode
                  ? (ERROR_LABELS[row.errorCode] ?? `失败：${row.errorCode}`)
                  : null}
                {row.state === 'DUPLICATE' && row.paperId ? (
                  <a href={`/app/papers/${encodeURIComponent(row.paperId)}`} target="_blank" rel="noopener noreferrer">打开已有论文（未新建副本）</a>
                ) : null}
                {row.state === 'IMPORTED' && row.jobId
                  ? <a href="/app/operations" target="_blank" rel="noopener noreferrer">已排队解析 · 查看进度</a>
                  : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table> : null}

      {failed.length ? (
        <div className={styles.failed}>
          <p data-testid="failed-note">
            {failed.length} 个文件失败，其他 {succeeded.length} 个不受影响。
          </p>
          <Button
            disabled={!failed.some(row => row.retryable !== false)}
            onClick={() => {
              queueRef.current?.retryFailed(failed.map((row) => row.key));
            }}
          >
            只重试失败文件
          </Button>
          {failed.some((row) => row.itemId) ? (
            <Button
              variant="quiet"
              disabled={requeueing}
              onClick={async () => {
                const ids = failed.map((row) => row.itemId).filter((id): id is string => Boolean(id));
                if (!batchId || !ids.length) return;
                setRequeueing(true); setBatchError(null); setRetryNotice(null);
                try { await api.retryImportItems(batchId, ids); setRetryNotice('已请求重新排队，可到「运行状态」查看。'); }
                catch (error) { setBatchError(errorCodeOf(error) ?? 'INTERNAL_001'); }
                finally { setRequeueing(false); }
              }}
            >
              让服务器重排已接收项
            </Button>
          ) : null}
        </div>
      ) : null}
      {retryNotice ? <p role="status">{retryNotice}</p> : null}

      {client.mode === 'TEST' ? (
        <p className="muted" data-testid="import-mode">
          当前为 TEST 模式（服务器未配置 Provider）。
        </p>
      ) : null}
    </Modal>
  );
}
