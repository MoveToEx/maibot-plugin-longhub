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
  Tags,
  Webhook,
} from 'lucide-react';
import { Link } from 'react-router';
import { api, formatDate, jobLabels } from '../api';
import { Badge, Button, Loading, Panel, PanelHeading } from '../components/ui';
import { useApp } from '../context';
import { useAction } from '../hooks';
import type { Status } from '../types';
import type { LucideIcon } from 'lucide-react';

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
  const jobs = s.tag_jobs;
  return (
    <div className="page-stack">
      <div className="overview-intro">
        <div className="flex items-center gap-2 text-xs text-accent font-medium">
          <span className={`status-dot ${s.ready && s.receiver.started ? 'online' : ''}`} />
          {s.ready && s.receiver.started ? '已准备好，与对话保持同频' : '从连接 LONG Hub 开始'}
        </div>
        <p className="mt-2 text-xs text-muted">表情来自远方，合适的回应就在这里。</p>
        <Link className="text-link" to="/catalog">
          浏览目录
          <ArrowRight size={14} />
        </Link>
      </div>
      <div className="metrics-grid" id="metrics">
        <Metric label="本地表情" value={s.total} note="仅保存元数据" icon={Images} />
        <Metric
          label="待处理通知"
          value={s.pending_notifications}
          note="Webhook 更新队列"
          icon={Webhook}
        />
        <Metric
          label="待标注图片"
          value={(jobs.queued || 0) + (jobs.running || 0)}
          note="按需与手工排队"
          icon={Sparkles}
        />
        <Metric label="已完成标注" value={jobs.done || 0} note="描述与标签已就绪" icon={Tags} />
      </div>
      <div className="grid xl:grid-cols-2 gap-5">
        <Panel>
          <PanelHeading
            title="目录同步"
            description="与 LONG Hub 保持一致的本地索引"
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
          <div className="panel-footer">
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
            <a
              className="text-link"
              href="https://docs.longhub.top/catalog-sync.html"
              target="_blank"
              rel="noreferrer"
            >
              同步协议
              <ArrowUpRight size={13} />
            </a>
          </div>
        </Panel>
        <Panel>
          <PanelHeading
            title="Webhook 接收器"
            description="接收更新通知，自动维护目录"
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
          <div className="panel-footer">
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
      <div className="grid lg:grid-cols-[1.5fr_1fr] gap-5">
        <Panel>
          <PanelHeading
            title={s.ready ? '一个表情，三份理解' : '首次连接，三步就绪'}
            description={
              s.ready
                ? '不同来源的标签共同帮助模型找到合适的回应。'
                : '完成配置后，目录会自动下载并校验。'
            }
          />
          <div className="steps-grid">
            {(s.ready
              ? [
                  ['LONG Hub', '同步作品、主体等上游标签。'],
                  ['视觉模型', '补充中文情绪、场景与龙脸标注。'],
                  ['人工修订', '补充标签，屏蔽误判，保留你的理解。'],
                ]
              : [
                  ['配置连接', '填写 Appkey 与 webhook 签名密钥。'],
                  ['启用订阅', '在 Hub 填写公网回调与请求体模板。'],
                  ['开始同步', '确认订阅启用并保存，即可导入目录。'],
                ]
            ).map(([title, text], i) => (
              <div key={title}>
                <span className="step-number">0{i + 1}</span>
                <h3 className="text-xs font-semibold mt-4">{title}</h3>
                <p className="text-xs text-muted leading-6 mt-2">{text}</p>
              </div>
            ))}
          </div>
        </Panel>
        <Panel>
          <PanelHeading title="目录分布" action={<Badge>{s.total.toLocaleString()} 张</Badge>} />
          <div className="space-y-5">
            {(['none', 'moderate', 'violent'] as const).map((rating, i) => (
              <div key={rating}>
                <div className="flex justify-between text-xs mb-2">
                  <span className="text-muted">{rating}</span>
                  <span className="tabular-nums">
                    {(s.ratings?.[rating] || 0).toLocaleString()}
                  </span>
                </div>
                <div className="distribution-track">
                  <span
                    style={{
                      width: `${s.total ? ((s.ratings?.[rating] || 0) / s.total) * 100 : 0}%`,
                      opacity: 1 - i * 0.23,
                    }}
                  />
                </div>
              </div>
            ))}
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
