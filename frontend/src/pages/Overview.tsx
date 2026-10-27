import {
  ArrowRight,
  ArrowUpRight,
  Database,
  Images,
  Layers3,
  RefreshCw,
  Webhook,
} from 'lucide-react';
import { Link } from 'react-router';
import { lazy, Suspense } from 'react';
import { api, formatDate, jobLabels } from '../api';
import { Badge, Button, Loading, Panel, PanelHeading } from '../components/ui';
import { useApp } from '../context';
import { useAction } from '../hooks';

const AnnotationChart = lazy(() =>
  import('../components/AnnotationChart').then(({ AnnotationChart }) => ({
    default: AnnotationChart,
  })),
);

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="detail-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
export function Overview() {
  const { status: s, notify, refresh } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  if (!s) return <Loading />;
  const syncBusy = ['queued', 'running'].includes(s.sync.status);
  const syncError = s.sync.error || s.sync_error;
  return (
    <div className="page-stack">
      <div className="overview-primary-row">
        <Panel id="sync-panel" className="flex flex-col">
          <PanelHeading
            title="目录同步"
            action={
              <Badge
                tone={
                  s.sync.status === 'failed'
                    ? 'danger'
                    : s.sync.status === 'done'
                      ? 'success'
                      : 'accent'
                }
              >
                {s.ready ? jobLabels[s.sync.status] : '等待配置'}
              </Badge>
            }
          />
          <div className="sync-visual">
            <span className="sync-node">
              <Database size={22} strokeWidth={1.4} />
              <small>本地目录</small>
            </span>
            <span className={`sync-track ${syncBusy ? 'sync-active' : ''}`}>
              <span />
              <RefreshCw size={16} className={syncBusy ? 'animate-spin' : ''} />
              <span />
            </span>
            <span className="sync-node accent">
              <Layers3 size={22} strokeWidth={1.4} />
              <small>LONG Hub</small>
            </span>
          </div>
          <dl>
            <Detail label="上次完整校准">{formatDate(s.last_sync?.completed_at)}</Detail>
            <Detail label="当前进度">
              {syncBusy
                ? s.sync.phase === 'checking_version'
                  ? '正在检查远端目录版本'
                  : `已导入 ${(s.sync.imported || 0).toLocaleString()} 条 · 刷新 ${s.sync.refreshed || 0} 张`
                : s.sync.skipped
                  ? '远端目录未变化，已跳过全量同步'
                  : '等待下一次校准'}
            </Detail>
          </dl>
          {syncError && (
            <p className="inline-error" role="alert">
              {syncError}
            </p>
          )}
          <div className="sync-actions">
            <Button
              variant="primary"
              disabled={pending || syncBusy || !s.ready || !s.receiver.started}
              onClick={() =>
                run(async () => {
                  await api.post('sync');
                  notify('目录校准已加入队列。');
                  refresh();
                })
              }
            >
              <RefreshCw size={14} />
              {syncBusy ? '校准进行中' : '立即校准'}
            </Button>
          </div>
        </Panel>
        <Panel id="catalog-total" className="catalog-total">
          <PanelHeading
            title="本地表情总数"
            action={<Images size={17} strokeWidth={1.6} className="text-muted" />}
          />
          <p className="catalog-total-number">{s.total.toLocaleString()}</p>
        </Panel>
      </div>
      <div className="overview-secondary-row">
        <Suspense
          fallback={
            <Panel>
              <Loading label="正在加载标注进度" />
            </Panel>
          }
        >
          <AnnotationChart status={s} />
        </Suspense>
        <Panel id="webhook-panel" className="flex flex-col">
          <PanelHeading
            title="Webhook 接收器"
            action={
              <Badge tone={s.receiver.started ? 'success' : 'danger'}>
                {s.receiver.started ? '正在监听' : '未启动'}
              </Badge>
            }
          />
          <div className="endpoint">
            <span className="method">POST</span>
            <code>{s.receiver.path}</code>
            <Webhook size={17} className="ml-auto text-muted" />
          </div>
          <dl>
            <Detail label="监听地址">
              <code>{s.receiver.listen}</code>
            </Detail>
            <Detail label="最近有效调用">{formatDate(s.last_webhook)}</Detail>
            <Detail label="运行状态">{s.receiver.error || s.background_error || '正常'}</Detail>
          </dl>
          <div className="webhook-actions">
            <Link className="button button-secondary" to="/logs">
              查看调用日志
              <ArrowRight size={14} />
            </Link>
            <Link className="text-link" to="/settings">
              连接设置
              <ArrowUpRight size={13} />
            </Link>
          </div>
        </Panel>
      </div>
    </div>
  );
}
