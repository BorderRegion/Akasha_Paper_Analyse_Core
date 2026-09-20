/**
 * TagActions (docs/06 §专题与实体 + UX-044).
 *
 * Confirming or merging tags PREVIEWS the effect first (affected papers, target
 * tag) and refuses to merge across conflicting semantics; the result reports the
 * affected count and never rewrites old evidence text.
 */

import { useEffect, useState, type ReactElement } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '../../components/ui/Button';
import { errorCodeOf } from '../../lib/apiError';
import { useSession } from '../../state/session';
import type { TagActionPreview } from '../../api/ui';
import styles from './saved.module.css';

export interface TagActionsProps {
  tags: { id: string; label: string }[];
}

export function TagActions({ tags }: TagActionsProps): ReactElement | null {
  const { api } = useSession();
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<string[]>([]);
  const tagIds = selected.filter(id => tags.some(tag => tag.id === id));
  const [target, setTarget] = useState('');
  const targetId = tagIds.includes(target) ? target : tagIds[0] ?? '';
  const [preview, setPreview] = useState<TagActionPreview | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [applied, setApplied] = useState<TagActionPreview | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const scope = JSON.stringify([tagIds, targetId]);
  useEffect(() => { setPreview(null); setConflict(null); setApplied(null); }, [scope]);
  if (!tags.length) return null;

  const run = async (action: 'confirm' | 'merge') => {
    setErrorCode(null);
    setConflict(null);
    setPending(true);
    try {
      const response = await api.tagAction({
        action,
        tag_ids: tagIds,
        target_tag_id: action === 'merge' ? targetId : null,
        preview_only: true,
      });
      if (response.data.conflict) {
        setConflict(response.data.conflict.reason);
        setPreview(response.data);
        return;
      }
      setPreview(response.data);
      setApplied(null);
    } catch (error) {
      setErrorCode(errorCodeOf(error));
    } finally {
      setPending(false);
    }
  };

  return (
    <section className={styles.wrap} aria-labelledby="tags-heading" data-testid="tag-actions">
      <h2 id="tags-heading">标签确认与归并</h2>
      <p className="muted">先预览影响范围，再执行；旧证据文本不会被修改。</p>
      <fieldset className={styles.tagChoices} disabled={pending}><legend className="srOnly">选择要整理的标签</legend>
        {tags.map(tag => <label key={tag.id}><input type="checkbox" checked={tagIds.includes(tag.id)} onChange={event => setSelected(current => event.target.checked ? [...current, tag.id] : current.filter(id => id !== tag.id))} />{tag.label}</label>)}
      </fieldset>
      {tagIds.length > 1 ? <label>归并到
        <select aria-label="归并到" value={targetId} disabled={pending} onChange={event => setTarget(event.target.value)}>
          {tags.filter(tag => tagIds.includes(tag.id)).map(tag => <option key={tag.id} value={tag.id}>{tag.label}</option>)}
        </select>
      </label> : null}
      <div className={styles.create}>
        <Button variant="quiet" disabled={pending || !tagIds.length} onClick={() => void run('confirm')}>
          确认所选标签（预览）
        </Button>
        <Button variant="quiet" disabled={pending || tagIds.length < 2} onClick={() => void run('merge')}>
          预览标签归并
        </Button>
      </div>

      {preview ? (
        <div data-testid="tag-preview">
          <p>
            影响 {preview.affected_count} 篇论文（{preview.affected_paper_ids.slice(0, 3).join('、')}
            {preview.affected_paper_ids.length > 3 ? ' 等' : ''}）
          </p>
          <p className="muted">{preview.note}</p>
          <p className="muted">已选：{tags.filter(tag => preview.tags.includes(tag.id)).map(tag => tag.label).join('、')}{preview.target_tag_id ? ` · 归并到：${tags.find(tag => tag.id === preview.target_tag_id)?.label ?? preview.target_tag_id}` : ''}</p>
          {!preview.conflict && !applied ? (
            <Button
              disabled={pending}
              onClick={async () => {
                setPending(true);
                setErrorCode(null);
                try {
                const response = await api.tagAction({
                  action: preview.action as 'confirm' | 'merge',
                  tag_ids: preview.tags,
                  target_tag_id: preview.target_tag_id,
                  preview_only: false,
                  idempotency_key: `tag:${preview.operation_id}`,
                });
                if (response.data.state !== 'COMPLETED') throw new Error('Tag action was not completed');
                setApplied(response.data);
                void queryClient.invalidateQueries({ queryKey: ['ui', 'library'] });
                } catch (error) {
                  setErrorCode(errorCodeOf(error) ?? 'INTERNAL_001');
                } finally {
                  setPending(false);
                }
              }}
            >
              确认执行
            </Button>
          ) : null}
        </div>
      ) : null}
      {conflict ? (
        <p role="alert" data-testid="tag-conflict">
          标签冲突：{conflict}。未做任何修改，请人工确认目标语义。
        </p>
      ) : null}
      {applied ? (
        <p role="status" data-testid="tag-applied">
          已应用，影响 {applied.affected_count} 篇；旧证据文本未修改。
        </p>
      ) : null}
      {errorCode ? (
        <p role="alert">标签请求未完成（{errorCode}），请刷新核对结果后重试。</p>
      ) : null}
    </section>
  );
}
