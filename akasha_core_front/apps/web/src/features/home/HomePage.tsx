/**
 * HomePage — research desk (docs/03 §S01).
 *
 * - "接着上次读" shows at most 3 papers, each with the STORED reading anchor
 *   (version + page) so继续 opens the version the user actually left, not the
 *   newest one, and never infers a position from job state;
 * - "需要你判断" shows at most 3 real items (failed imports / disputed claims);
 * - an empty library gets an import button and one explanatory line — no fake
 *   "welcome back", no invented interests.
 */

import { useMemo, type ReactElement } from 'react';
import { Link } from 'react-router-dom';
import type { LibraryQuery } from '../../api/contract';
import { AsyncBoundary } from '../../components/domain/AsyncBoundary';
import { Button } from '../../components/ui/Button';
import { useLibrary } from '../../state/queries';
import { queryToSearch } from '../../state/libraryUrl';
import { useImportDialog } from '../import/importContext';
import styles from './home.module.css';
import { errorCodeOf } from '../../lib/apiError';
import { QuietMark } from '../../components/ui/QuietMark';

export interface HomePageProps {
  onOpenImport?(): void;
}

const CONTINUE_QUERY: LibraryQuery = {
  query: '',
  kind: 'PAPERS',
  filters: { read_states: ['READING', 'READ'] },
  sort: 'RECENT',
  cursor: null,
  limit: 12,
};

/** One-row probe WITHOUT filters: "is the library empty?" is a different
 * question from "is there reading history?" and must not be conflated. */
const LIBRARY_PROBE_QUERY: LibraryQuery = {
  query: '',
  kind: 'PAPERS',
  filters: {},
  sort: 'RECENT',
  cursor: null,
  limit: 1,
};

const ATTENTION_QUERY: LibraryQuery = {
  query: '',
  kind: 'PAPERS',
  filters: { audit_states: ['DISPUTED', 'UNSUPPORTED'] },
  sort: 'RECENT',
  cursor: null,
  limit: 3,
};

export function HomePage({ onOpenImport }: HomePageProps): ReactElement {
  const importDialog = useImportDialog();
  const openImport = onOpenImport ?? importDialog.open;
  const reading = useLibrary(CONTINUE_QUERY);
  const attention = useLibrary(ATTENTION_QUERY);
  const libraryProbe = useLibrary(LIBRARY_PROBE_QUERY);

  const continueItems = useMemo(
    () =>
      (reading.data?.page.kind === 'PAPERS' ? reading.data.page.items : [])
        .filter((item) => item.personal.reading_anchor)
        .slice(0, 3),
    [reading.data],
  );

  // The library is empty only when an UNFILTERED probe reports zero; a filtered
  // zero means "no reading history yet", which is a different message.
  const libraryEmpty = libraryProbe.isSuccess && libraryProbe.data?.page.total === 0;

  return (
    <section className={styles.page} aria-labelledby="home-heading">
      <header className={styles.pageHeader}><div><p className="eyebrow">THE READING ROOM</p><h1 id="home-heading">研究桌面</h1></div><span className={styles.headerNote}>把目光，留在值得读的地方。</span></header>
      <section className={styles.hero} aria-label="阅读书房">
        <div className={styles.heroCopy}><span className="eyebrow">一页，一点新的想法</span><h2>今天，想读些什么？</h2><p>论文放在这里就好。<br />读到哪里，想到什么，都可以慢慢记下来。</p><div className={styles.heroActions}><Button onClick={openImport}>导入 PDF <span aria-hidden="true">↗</span></Button><Link to="/app/library">去文献库 <span aria-hidden="true">→</span></Link></div></div>
        <QuietMark className={styles.heroMark} />
      </section>
      <div className={styles.deskGrid}>
      <section className={styles.block} aria-labelledby="continue-heading">
        <h2 id="continue-heading">接着上次读</h2>
        <AsyncBoundary
          state={reading.isPending ? 'loading' : reading.isError ? 'error' : 'ready'}
          errorCode={errorCodeOf(reading.error)}
          scopeLabel="在读与已读文献"
          onRetry={() => void reading.refetch()}
        >
          {!reading.data ? null : continueItems.length ? (
            <ul className={styles.cards}>
              {continueItems.map((item) => {
                const anchor = item.personal.reading_anchor;
                return (
                  <li key={item.paper_id} className={styles.card}>
                    <h3>{item.title}</h3>
                    <p className="muted">
                      上次读到第 {anchor?.page_number ?? '—'} 页
                      {anchor?.section_id ? ` · 章节 ${anchor.section_id}` : ''}
                    </p>
                    <Link
                      to={`/app/papers/${item.paper_id}?paper_version_id=${
                        anchor?.paper_version_id ?? item.paper_version_id
                      }&reader=1${anchor?.page_number ? `&page=${anchor.page_number}` : ''}`}
                    >
                      继续阅读 →
                    </Link>
                  </li>
                );
              })}
            </ul>
          ) : libraryEmpty ? (
            <div data-testid="empty-library">
              <p>文献库还是空的。导入一篇 PDF 就能开始。</p>
            </div>
          ) : (
            <div data-testid="no-reading-history">
              <p>还没有阅读记录。挑一篇开始吧。</p>
              <Link to={`/app/library?${queryToSearch({ ...CONTINUE_QUERY, filters: {} })}`}>
                开始一篇 →
              </Link>
            </div>
          )}
        </AsyncBoundary>
      </section>

      <section className={styles.block} aria-labelledby="attention-heading">
        <h2 id="attention-heading">需要你判断</h2>
        <AsyncBoundary
          state={attention.isPending ? 'loading' : attention.isError ? 'error' : 'ready'}
          errorCode={errorCodeOf(attention.error)}
          scopeLabel="存在争议或证据不足的文献"
          onRetry={() => void attention.refetch()}
        >
          {!attention.data ? null : attention.data.page.kind === 'PAPERS' && attention.data.page.items.length ? (
            <ul className={styles.cards}>
              {attention.data.page.items.slice(0, 3).map((item) => (
                <li key={item.paper_id} className={styles.card}>
                  <h3>{item.title}</h3>
                  <p className="muted">
                    需要再看一眼：{Object.entries(item.audit.by_state)
                      .filter(([state]) => state === 'DISPUTED' || state === 'UNSUPPORTED')
                      .map(([state, count]) => `${state === 'DISPUTED' ? '存在分歧' : '证据不足'} ${count} 条`)
                      .join('，') || '未报告'}
                  </p>
                  <Link to={`/app/papers/${item.paper_id}?tab=audit`}>打开核查页</Link>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted" data-testid="attention-empty">
              暂时没有需要你核查的结论。
            </p>
          )}
        </AsyncBoundary>
      </section>
      </div>
      <section className={styles.block} aria-labelledby="topics-heading">
        <h2 id="topics-heading">当前专题</h2>
        <div className={styles.topicLink}><div><p>把相关的论文，放在一起。</p><span className="muted">围绕一个问题整理阅读，也许会看见新的联系。</span></div><Link to="/app/collections">整理专题 <span aria-hidden="true">↗</span></Link></div>
      </section>
    </section>
  );
}
