import {
  ArrowRight,
  ArrowUpRight,
  Check,
  CheckCircle2,
  CircleDashed,
  Database,
  Images,
  Layers3,
  Pause,
  Play,
  RefreshCw,
  Sparkles,
  Webhook,
} from 'lucide-react';
import { Link } from 'react-router';
import { lazy, Suspense } from 'react';
import { api, formatDate, jobLabels } from '../api';
import { Badge, Button, Loading, Panel, PanelHeading } from '../components/ui';
import { useApp } from '../context';
import { useAction } from '../hooks';
import type { Status } from '../types';
import type { LucideIcon } from 'lucide-react';

const AnnotationChart = lazy(() =>
  import('../components/AnnotationChart').then(({ AnnotationChart }) => ({
    default: AnnotationChart,
  })),
);

function Metric({
  label,
  value,
  note,
  icon: Icon,
}: {
  label: string;
  value: number;
  note: string;
  icon: LucideIcon;
}) {
  return (
    <div className="metric">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted">{label}</span>
        <Icon size={16} strokeWidth={1.6} className="text-muted" />
      </div>
      <p className="metric-number">{value.toLocaleString()}</p>
      <p className="text-[11px] text-muted flex items-center gap-1.5">
        <span className="small-dot" />
        {note}
      </p>
    </div>
  );
}
function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="detail-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}
function JobMetrics({ status }: { status: Status }) {
  const jobs = status.tag_jobs;
  return (
    <div className="metrics-grid">
      <Metric label="排队中" value={jobs.queued || 0} note="等待模型调用" icon={Layers3} />
      <Metric label="处理中" value={jobs.running || 0} note="正在生成标注" icon={Sparkles} />
      <Metric label="已完成" value={jobs.done || 0} note="已生成描述与标签" icon={CheckCircle2} />
      <Metric
        label="待重试"
        value={jobs.failed || 0}
        note="在目录详情中重新标注"
        icon={RefreshCw}
      />
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
                ? `已导入 ${(s.sync.imported || 0).toLocaleString()} 条 · 刷新 ${s.sync.refreshed || 0} 张`
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
                  notify('全量校准已加入队列。');
                  refresh();
                })
              }
            >
              <RefreshCw size={14} />
              {syncBusy ? '校准进行中' : '立即全量校准'}
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
export function Tasks() {
  const { status: s, notify, refresh } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  if (!s) return <Loading />;
  return (
    <div className="page-stack">
      <JobMetrics status={s} />
      <Panel>
        <PanelHeading
          title="Embedding 语义检索"
          description="把文本标注转成向量，按概念寻找合适的龙图。"
          action={
            <Badge
              tone={s.semantic?.available ? 'success' : s.semantic?.error ? 'danger' : 'neutral'}
            >
              {
                (
                  {
                    unconfigured: '未配置',
                    empty: '目录为空',
                    indexing: '索引同步中',
                    insufficient: '标注覆盖率不足',
                    ready: '语义工具已开放',
                    error: '索引异常',
                  } as Record<string, string>
                )[s.semantic?.state || 'unconfigured']
              }
            </Badge>
          }
        />
        <div className="grid sm:grid-cols-3 gap-5 my-6">
          <div>
            <p className="text-xs text-muted">有效标注覆盖率</p>
            <p className="text-2xl font-semibold tabular-nums mt-2">
              {((s.semantic?.coverage || 0) * 100).toFixed(1)}%
            </p>
            <p className="text-[11px] text-muted mt-2">
              {s.semantic?.annotated || 0} / {s.semantic?.total || 0} 张可发送图片
            </p>
          </div>
          <div>
            <p className="text-xs text-muted">已向量化</p>
            <p className="text-2xl font-semibold tabular-nums mt-2">{s.semantic?.indexed || 0}</p>
            <p className="text-[11px] text-muted mt-2">待索引 {s.semantic?.pending || 0} 张</p>
          </div>
          <div>
            <p className="text-xs text-muted">嵌入模型</p>
            <p className="text-sm font-medium break-all mt-3">{s.semantic?.model || '尚未选择'}</p>
            <p className="text-[11px] text-muted mt-2">AstrBot 内置向量库</p>
          </div>
        </div>
        <div
          className="distribution-track"
          role="progressbar"
          aria-label="有效标注覆盖率"
          aria-valuenow={Math.round((s.semantic?.coverage || 0) * 100)}
          aria-valuemin={0}
          aria-valuemax={100}
        >
          <span style={{ width: `${(s.semantic?.coverage || 0) * 100}%` }} />
        </div>
        {s.semantic?.error && (
          <p className="inline-error" role="alert">
            {s.semantic.error}
          </p>
        )}
        <div className="panel-footer">
          <p className="text-xs text-muted leading-7 flex-1">
            覆盖率严格超过 50%
            且索引就绪后开放语义工具，由模型根据检索意图选择使用。重建会重新调用嵌入模型。
          </p>
          <Button
            id="rebuild-semantic"
            disabled={pending || !s.semantic?.configured || s.semantic?.state === 'indexing'}
            onClick={() =>
              run(async () => {
                await api.post('semantic/rebuild');
                notify('向量索引重建已排队，后台将重新向量化文本标注。');
                refresh();
              })
            }
          >
            <RefreshCw size={14} />
            重建向量索引
          </Button>
        </div>
      </Panel>
      <Panel>
        <PanelHeading
          title="视觉标签补全"
          description="复用 AstrBot 中的视觉提供商，按既定速率在后台处理。"
          action={
            <Badge tone={s.vision_configured && !s.tagging_paused ? 'success' : 'warning'}>
              {!s.vision_configured
                ? '尚未选择模型'
                : s.tagging_paused
                  ? '队列已暂停'
                  : '队列运行中'}
            </Badge>
          }
        />
        <div className="task-callout">
          <span className="feature-icon">
            <Sparkles size={25} strokeWidth={1.4} />
          </span>
          <div>
            <h3 className="text-sm font-medium">让模型看懂每张表情</h3>
            <p className="text-xs text-muted leading-7 mt-2">
              补充画面描述、交流意图与中文标签。贴脸素材使用「龙脸」命名，中英文标签共同参与检索。
            </p>
          </div>
        </div>
        {!s.vision_configured && (
          <p className="inline-hint">
            先在
            <Link to="/settings" className="text-accent underline underline-offset-4">
              连接与设置
            </Link>
            中选择支持图片输入的视觉提供商。
          </p>
        )}
        <div className="flex flex-wrap items-center gap-3 mt-6">
          <Button
            variant="primary"
            id="tag-missing"
            disabled={pending || !s.vision_configured}
            onClick={() =>
              run(async () => {
                const result = await api.post<{ queued: number }>('tagging/enqueue');
                notify(`已加入 ${result.queued} 个标注任务。`);
                refresh();
              })
            }
          >
            <Sparkles size={14} />
            为缺少有效标注的图片排队
          </Button>
          <Button
            id="pause-tagging"
            disabled={pending}
            onClick={() =>
              run(async () => {
                await api.post('tagging/pause', { paused: !s.tagging_paused });
                notify(s.tagging_paused ? '标注队列已继续。' : '标注队列已暂停，在途请求会完成。');
                refresh();
              })
            }
          >
            {s.tagging_paused ? <Play size={14} /> : <Pause size={14} />}
            {s.tagging_paused ? '继续队列' : '暂停队列'}
          </Button>
        </div>
        <p className="text-xs text-muted leading-7 mt-4">
          排队会调用你配置的模型。人工标签和描述独立保留；失败三次的条目可在目录详情中重新标注。
        </p>
      </Panel>
      <Panel>
        <PanelHeading
          title="标签如何协作"
          description="有效标签 = 上游标签 + 视觉模型标签 + 人工标签 − 屏蔽标签"
        />
        <div className="source-cards">
          {[
            ['Hub 标签', '英文作品与主体标签', 'source-hub'],
            ['视觉模型', '中文情绪、画面与场景', 'source-ai'],
            ['人工修订', '补充与纠正，始终保留', 'source-manual'],
          ].map(([title, text, color]) => (
            <div className="source-card" key={title}>
              <span className={`tag ${color}`}>{title}</span>
              <p className="text-xs text-muted mt-3">{text}</p>
              <Check size={15} className="mt-5 text-muted" />
            </div>
          ))}
        </div>
        <p className="text-xs text-muted leading-7 mt-5 flex gap-2">
          <CircleDashed size={15} className="shrink-0 mt-1" />
          图片、文字或标注提示版本变化后，旧标注仍可查询，并会显示为过期以便重新排队。
        </p>
      </Panel>
    </div>
  );
}
