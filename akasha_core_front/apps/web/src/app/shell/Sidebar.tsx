import type { ReactElement } from 'react';
import { NavLink } from 'react-router-dom';
import styles from '../shell/shell.module.css';
import { NavGlyph } from '../../components/ui/NavGlyph';

export interface NavItem {
  to: string;
  label: string;
}

/** Primary navigation (docs/02). Compare opens from the selection tray, so it
 * is deliberately NOT a nav item; operations/settings sit in their own group. */
export const PRIMARY_NAV: NavItem[] = [
  { to: '/app/home', label: '桌面' },
  { to: '/app/library', label: '文献库' },
  { to: '/app/collections', label: '研究专题' },
  { to: '/app/techniques', label: '方法与技巧' },
  { to: '/app/review', label: '待核查' },
];

export const SECONDARY_NAV: NavItem[] = [
  { to: '/app/operations', label: '运行状态' },
  { to: '/app/settings', label: '设置' },
];

function renderGroup(items: NavItem[], className?: string): ReactElement {
  return (
    <nav className={className} aria-label={className ? '次级导航' : '主导航'}>
      <ul className={styles.navGroup} style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {items.map((item) => (
          <li key={item.to}>
            <NavLink to={item.to} className={styles.navLink ?? 'navLink'}>
              <NavGlyph name={item.to.split('/').pop() ?? 'home'} />
              <span>{item.label}</span>
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function Sidebar(): ReactElement {
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}><span className={styles.brandSeal} aria-hidden="true">A</span><div>Akasha<span className={styles.brandEnglish}>研究工作台</span></div></div>
      <p className={styles.navCaption}>我的书房</p>
      {renderGroup(PRIMARY_NAV)}
      {renderGroup(SECONDARY_NAV, styles.navSecondary)}
      <div className={styles.sidebarNote}><span aria-hidden="true">♮</span><p>不用着急。<br />一篇一篇，慢慢读。</p><small>A LITTLE ROOM FOR THOUGHT</small></div>
    </aside>
  );
}
