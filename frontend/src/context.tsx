import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useLocation } from 'react-router';
import { api } from './api';
import { useResource } from './hooks';
import type { Status } from './types';

type Notice = { text: string; error: boolean } | null;
interface AppState {
  status?: Status;
  statusError: string;
  revision: number;
  refresh: () => void;
  notify: (text: string, error?: boolean) => void;
  notice: Notice;
  dismissNotice: () => void;
}
const Context = createContext<AppState | null>(null);
export function AppProvider({ children }: { children: ReactNode }) {
  const status = useResource(() => api.get<Status>('status'), []);
  const [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState<Notice>(null);
  const { pathname } = useLocation();
  const notify = useCallback((text: string, error = false) => setNotice({ text, error }), []);
  const refresh = useCallback(() => {
    status.reload();
    setRevision((x) => x + 1);
  }, [status.reload]);
  useEffect(() => {
    if (!notice || notice.error) return;
    const timer = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);
  useEffect(() => {
    if (!['/overview', '/tasks'].includes(pathname)) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) status.reload();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [pathname, status.reload]);
  return (
    <Context.Provider
      value={{
        status: status.data,
        statusError: status.error,
        revision,
        refresh,
        notify,
        notice,
        dismissNotice: () => setNotice(null),
      }}
    >
      {children}
    </Context.Provider>
  );
}
export function useApp() {
  const value = useContext(Context);
  if (!value) throw new Error('LONG Hub panel context is unavailable');
  return value;
}
