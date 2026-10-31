import {
  CheckCircle2,
  CircleHelp,
  Images,
  LayoutDashboard,
  RefreshCw,
  Settings2,
  Sparkles,
  Webhook,
  X,
} from 'lucide-react';
import { useEffect } from 'react';
import { NavLink, Navigate, Route, Routes, useLocation } from 'react-router';
import { api } from './api';
import { AppProvider, useApp } from './context';
import { useResource } from './hooks';
import { Button, ErrorState, Loading } from './components/ui';
import { Overview } from './pages/Overview';
import { Tasks } from './pages/Tasks';
import { Catalog, MemeDetail } from './pages/Catalog';
import { Logs } from './pages/Logs';
import { Settings } from './pages/Settings';

const navigation = [
  {
    path: 'overview',
    title: '运行概览',
    icon: LayoutDashboard,
  },
  {
    path: 'catalog',
    title: '表情目录',
    icon: Images,
  },
  { path: 'tasks', title: '标注任务', icon: Sparkles },
  {
    path: 'logs',
    title: 'Webhook 日志',
    icon: Webhook,
  },
  {
    path: 'settings',
    title: '连接与设置',
    icon: Settings2,
  },
];

function Shell() {
  const { pathname } = useLocation();
  const page = navigation.find((item) => pathname.startsWith(`/${item.path}`)) ?? navigation[0];
  const { status, statusError, refresh, notice, dismissNotice } = useApp();
  const connected = !!status?.ready && status.receiver.started;
  useEffect(() => {
    document.title = `${page.title} · LONG Hub`;
    window.scrollTo({ top: 0, left: 0 });
  }, [page.title]);
  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById('main-content')?.focus();
        }}
      >
        跳至内容
      </a>
      <header className="topbar">
        <div className="topbar-inner">
          <nav className="navigation" aria-label="管理导航">
            {navigation.map((item) => (
              <NavLink
                key={item.path}
                to={`/${item.path}`}
                data-tab={item.path}
                className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
              >
                <item.icon size={16} strokeWidth={1.7} />
                <span>{item.title}</span>
                {item.path === 'tasks' && !!status?.tag_jobs.queued && (
                  <span className="nav-count">{status.tag_jobs.queued}</span>
                )}
              </NavLink>
            ))}
          </nav>
          <div className="topbar-actions">
            <span className="topbar-status">
              <span className={`status-dot ${connected ? 'online' : ''}`} />
              <span id="connection-state">
                {!status
                  ? '正在连接'
                  : connected
                    ? '已连接接收器'
                    : status.ready
                      ? '接收器异常'
                      : '等待首次配置'}
              </span>
            </span>
            <a
              href="https://docs.longhub.top"
              target="_blank"
              rel="noreferrer"
              className="icon-button"
              aria-label="文档与帮助"
              title="文档与帮助"
            >
              <CircleHelp size={16} />
            </a>
          </div>
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="main-content">
        <div className="page-heading">
          <div>
            <p className="eyebrow">LONG HUB CONSOLE</p>
            <h1>{page.title}</h1>
          </div>
          <Button id="refresh" onClick={refresh}>
            <RefreshCw size={14} />
            刷新
          </Button>
        </div>
        {statusError && <ErrorState message={statusError} retry={refresh} />}
        <Routes>
          <Route path="/overview" element={<Overview />} />
          <Route path="/catalog" element={<Catalog />}>
            <Route path=":id" element={<MemeDetail />} />
          </Route>
          <Route path="/logs" element={<Logs />} />
          <Route path="/tasks" element={<Tasks />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/overview" replace />} />
        </Routes>
      </main>
      {notice && (
        <div
          className={`notice ${notice.error ? 'notice-error' : ''}`}
          role={notice.error ? 'alert' : 'status'}
        >
          <span className="notice-icon">{notice.error ? '!' : <CheckCircle2 size={18} />}</span>
          <p>{notice.text}</p>
          <Button
            variant="ghost"
            className="icon-button"
            onClick={dismissNotice}
            aria-label="关闭通知"
          >
            <X size={15} />
          </Button>
        </div>
      )}
    </div>
  );
}

export default function App() {
  const ready = useResource(async () => {
    await api.ready();
    return true;
  }, []);
  if (ready.error)
    return (
      <div className="bootstrap">
        <h1 className="text-xl font-semibold mb-4">LONG Hub 表情目录</h1>
        <ErrorState message={ready.error} retry={ready.reload} />
      </div>
    );
  if (!ready.data) return <Loading label="正在连接 LONG Hub 插件" />;
  return (
    <AppProvider>
      <Shell />
    </AppProvider>
  );
}
