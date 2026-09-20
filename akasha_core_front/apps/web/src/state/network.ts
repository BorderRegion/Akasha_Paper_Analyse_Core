/**
 * Offline behaviour (docs/03 §S03 + docs/05, UX-053).
 *
 * When the connection drops:
 * - the last successful snapshot STAYS on screen, marked stale with its time;
 * - local drafts keep working and are never silently discarded;
 * - the app NEVER switches to fixtures/Demo data: a failure stays a failure;
 * - when the connection returns, the pending requests can be retried explicitly.
 */

import { useEffect, useState } from 'react';

export interface NetworkStatus {
  online: boolean;
  lastOnlineAt: string | null;
}

export function useNetworkStatus(): NetworkStatus {
  const [online, setOnline] = useState<boolean>(
    typeof navigator === 'undefined' ? true : navigator.onLine !== false,
  );
  const [lastOnlineAt, setLastOnlineAt] = useState<string | null>(
    online ? new Date().toISOString() : null,
  );

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const goOnline = () => {
      setOnline(true);
      setLastOnlineAt(new Date().toISOString());
    };
    const goOffline = () => setOnline(false);
    window.addEventListener('online', goOnline);
    window.addEventListener('offline', goOffline);
    return () => {
      window.removeEventListener('online', goOnline);
      window.removeEventListener('offline', goOffline);
    };
  }, []);

  return { online, lastOnlineAt };
}

/**
 * Whether a payload may be shown while offline. Only data that really came from
 * the server qualifies: there is no fixture fallback anywhere in this app.
 */
export function canRenderOffline(payloadSource: 'server' | 'fixture' | 'none'): boolean {
  return payloadSource === 'server';
}

export const OFFLINE_NOTICE =
  '连接中断，仍保留上次结果。恢复后请重试；不会切换到演示数据。';
