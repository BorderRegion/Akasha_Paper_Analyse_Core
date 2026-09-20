import { useState, type ReactElement, type ReactNode } from 'react';
import { useSession } from '../../state/session';
import { Button } from '../../components/ui/Button';
import { errorCodeOf } from '../../lib/apiError';
import { QuietMark } from '../../components/ui/QuietMark';
import styles from './auth.module.css';

/** Do not mount private routes/queries until the cookie session is restored. */
export function SessionGate({ children }: { children: ReactNode }): ReactElement {
  const { status, errorCode, login } = useSession();
  const [token, setToken] = useState('');
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  if (status === 'authenticated') return <>{children}</>;
  if (status === 'loading') return <main className={styles.waiting}><p role="status">稍等。正在打开书房…</p></main>;
  if (status === 'error') {
    return <main className={styles.waiting}><div><h1>暂时无法连接</h1><p role="alert">书房暂时没有回应。错误编号：{errorCode}</p>
      <Button onClick={() => window.location.reload()}>重新连接</Button></div></main>;
  }
  return (
    <main className={styles.page}>
      <aside className={styles.scene} aria-label="静研书房">
        <div className={styles.wordmark}>Akasha <span>研究工作台</span></div>
        <QuietMark className={styles.botanical} />
        <div className={styles.sceneText}><span className={styles.small}>A QUIET PLACE TO READ</span><h2>外面有些吵。<br />在这里，读一会儿吧。</h2><p>把论文、笔记，还有没想明白的事，<br />留在同一个地方。</p></div>
        <span className={styles.edition}>若叶 · quiet green edition</span>
      </aside>
      <section className={styles.panel}>
      <div className={styles.formWrap}>
      <p className="eyebrow">YOUR PRIVATE LIBRARY</p>
      <h1>书房在这里。</h1>
      <p className={styles.intro}>输入登录口令，就可以继续阅读了。</p>
      <form onSubmit={async (event) => {
        event.preventDefault();
        if (pending || !token.trim()) return;
        setPending(true);
        setFailure(null);
        const supplied = token.trim();
        try { await login(supplied); setToken(''); setVisible(false); }
        catch (error) { setFailure(errorCodeOf(error) ?? 'INTERNAL_001'); }
        finally { setPending(false); }
      }}>
        <label htmlFor="application-token">登录口令 <span className={styles.labelHint}>应用 token</span></label>
        <div className={styles.inputRow}>
        <input id="application-token" className="input" type={visible ? 'text' : 'password'}
          aria-label="应用 token" aria-describedby="login-help" autoComplete="current-password" autoCapitalize="none" spellCheck={false} value={token} disabled={pending}
          placeholder="粘贴你的登录口令"
          onChange={(event) => setToken(event.target.value)} />
        <button type="button" className={styles.reveal} onClick={() => setVisible(v => !v)} aria-pressed={visible}>{visible ? '隐藏' : '显示'}</button>
        </div>
        <Button type="submit" disabled={pending || !token.trim()}>
          {pending ? '登录中…' : '登录'}
        </Button>
        {failure ? <p role="alert">{failure === 'AUTH_001' ? '口令没有对上。检查一下有没有漏字，再试一次。' : failure === 'RATE_LIMITED' ? '刚才试了几次，稍等一分钟再登录。' : '暂时没能连上书房。请稍后再试。'}<small className={styles.errorCode}>错误编号：{failure}</small></p> : null}
      </form>
      <p id="login-help" className={styles.help}>口令由部署时生成，和模型 API Key 不同。<br />复制时多出的首尾空格会自动去掉。</p>
      </div>
      <p className={styles.privacy}>只属于你的阅读空间。</p>
      </section>
    </main>
  );
}
