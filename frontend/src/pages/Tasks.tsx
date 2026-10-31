import { Switch } from '@base-ui/react/switch';
import { Tooltip } from '@base-ui/react/tooltip';
import { ListPlus, LoaderCircle, Pause, Play, RotateCcw } from 'lucide-react';
import { lazy, Suspense, useState } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { Link } from 'react-router';
import { api } from '../api';
import { Badge, Button, Loading, Modal, Panel, PanelHeading } from '../components/ui';
import { useApp } from '../context';
import { useAction } from '../hooks';

const EmbeddingProgress = lazy(() =>
  import('../components/AnnotationChart').then(({ EmbeddingProgress }) => ({
    default: EmbeddingProgress,
  })),
);
const labels = ['未标注', '已标注未向量化', '已向量化'];
const colors = ['--chart-pending', '--chart-annotated', '--chart-indexed'];

function IconAction({
  label,
  tip,
  children,
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; tip: string; children: ReactNode }) {
  return (
    <Tooltip.Root>
      <Tooltip.Trigger
        type="button"
        className={`task-icon-button ${className}`}
        aria-label={label}
        delay={150}
        {...props}
      >
        {children}
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Positioner side="top" align="end" sideOffset={8} className="z-50">
          <Tooltip.Popup className="tooltip-popup" role="tooltip">
            {tip}
          </Tooltip.Popup>
        </Tooltip.Positioner>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

export function Tasks() {
  const { status: s, notify, refresh } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  const [confirmReset, setConfirmReset] = useState(false);
  if (!s) return <Loading />;
  const counts = s.semantic?.catalog;
  const values = [
    counts?.unannotated ?? s.total,
    counts?.annotated_unindexed ?? 0,
    counts?.indexed ?? 0,
  ];
  const total = values.reduce((sum, value) => sum + value, 0);
  const completion = total ? ((values[1] + values[2]) / total) * 100 : 0;
  const queue = s.tag_queue || [];
  return (
    <Tooltip.Provider>
      <div className="tasks-layout">
        <Panel id="tagging-progress" className="task-panel">
          <PanelHeading
            title="图库标注"
            action={
              <Badge
                tone={!s.vision_configured ? 'warning' : s.tagging_paused ? 'neutral' : 'success'}
              >
                {!s.vision_configured
                  ? '未配置视觉模型'
                  : s.tagging_paused
                    ? '队列已暂停'
                    : '队列运行中'}
              </Badge>
            }
          />
          <div className="tagging-body">
            <div className="annotation-overview">
              <div className="flex items-end justify-between gap-3 mb-5">
                <span className="text-xs text-muted">当前图库 · {total.toLocaleString()} 张</span>
                <span className="text-sm font-medium tabular-nums">
                  {completion.toFixed(1)}% 已标注
                </span>
              </div>
              <div
                className="annotation-track"
                role="progressbar"
                aria-label="图库标注进度"
                aria-valuenow={Math.round(completion)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuetext={labels
                  .map((label, index) => `${label} ${values[index]} 张`)
                  .join('，')}
              >
                {values.map((value, index) => (
                  <span
                    key={labels[index]}
                    style={{
                      width: `${total ? (value / total) * 100 : 0}%`,
                      background: `var(${colors[index]})`,
                    }}
                  />
                ))}
              </div>
              <ul className="annotation-key" aria-label="图库标注状态">
                {values.map((value, index) => (
                  <li key={labels[index]}>
                    <span className="flex items-center gap-1.5">
                      <span className="chart-dot" style={{ background: `var(${colors[index]})` }} />
                      {labels[index]}
                    </span>
                    <strong>{value.toLocaleString()}</strong>
                  </li>
                ))}
              </ul>
            </div>
            <div className="tagging-queue">
              <p className="text-xs font-medium mb-3">
                标注队列 <span className="text-muted font-normal ml-1">前 5 项</span>
              </p>
              {queue.length ? (
                <ol aria-label="标注队列前五项">
                  {queue.slice(0, 5).map((job) => (
                    <li key={job.image_id}>
                      <Link
                        to={`/catalog/${job.image_id}`}
                        className="queue-item"
                        title={`${job.text || '无图片文字'} · ${job.image_id}`}
                        aria-label={`查看标注任务 ${job.image_id}`}
                      >
                        {job.status === 'running' ? (
                          <LoaderCircle size={13} className="text-accent animate-spin shrink-0" />
                        ) : (
                          <span className="queue-dot" />
                        )}
                        <span className="min-w-0">
                          <span className="block truncate text-xs">
                            {job.text || `#${job.image_id}`}
                          </span>
                          <span className="block text-[10px] text-muted mt-1">
                            {job.status === 'running'
                              ? '正在标注'
                              : job.attempts
                                ? '等待重试'
                                : '等待标注'}
                          </span>
                        </span>
                      </Link>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-xs text-muted py-7">暂无排队任务</p>
              )}
            </div>
          </div>
          <div className="task-actions">
            <label className="task-auto-toggle">
              <span>自动标注</span>
              <Switch.Root
                aria-label="自动标注"
                className="switch"
                checked={!!s.tagging_auto}
                disabled={pending || !s.vision_configured}
                onCheckedChange={(enabled) =>
                  run(async () => {
                    await api.post('tagging/auto', { enabled });
                    notify(
                      enabled ? '自动标注已开启，未完成标注的图片将持续入队。' : '自动标注已关闭。',
                    );
                    refresh();
                  })
                }
              >
                <Switch.Thumb className="switch-thumb" />
              </Switch.Root>
            </label>
            <span className="task-action-divider" />
            <IconAction
              id="pause-tagging"
              label={s.tagging_paused ? '继续标注队列' : '暂停标注队列'}
              tip={s.tagging_paused ? '继续标注队列' : '暂停标注队列，进行中的任务会完成'}
              disabled={pending}
              onClick={() =>
                run(async () => {
                  await api.post('tagging/pause', { paused: !s.tagging_paused });
                  notify(s.tagging_paused ? '标注队列已继续。' : '标注队列已暂停。');
                  refresh();
                })
              }
            >
              {s.tagging_paused ? <Play size={16} /> : <Pause size={16} />}
            </IconAction>
            <IconAction
              id="tag-missing"
              label="将所有需要标注的图片入队"
              tip="将所有缺少有效标注的图片入队，并重试失败任务"
              disabled={pending || !s.vision_configured}
              onClick={() =>
                run(async () => {
                  const result = await api.post<{ queued: number }>('tagging/enqueue', {
                    force: true,
                  });
                  notify(`已加入 ${result.queued} 个标注任务。`);
                  refresh();
                })
              }
            >
              <ListPlus size={17} />
            </IconAction>
          </div>
        </Panel>
        <Suspense
          fallback={
            <Panel>
              <Loading label="正在加载 Embedding 进度" />
            </Panel>
          }
        >
          <EmbeddingProgress
            status={s}
            actions={
              <>
                <IconAction
                  id="pause-embedding"
                  label={s.semantic?.paused ? '继续 Embedding' : '暂停 Embedding'}
                  tip={
                    s.semantic?.paused
                      ? '继续后台向量化'
                      : '暂停新的后台向量化任务，进行中的批次会完成'
                  }
                  disabled={pending || !s.semantic?.configured}
                  onClick={() =>
                    run(async () => {
                      await api.post('semantic/pause', { paused: !s.semantic?.paused });
                      notify(s.semantic?.paused ? '后台向量化已继续。' : '后台向量化已暂停。');
                      refresh();
                    })
                  }
                >
                  {s.semantic?.paused ? <Play size={16} /> : <Pause size={16} />}
                </IconAction>
                <IconAction
                  id="rebuild-semantic"
                  className="task-icon-button-danger"
                  label="重置所有 Embedding"
                  tip="重置全部向量。未暂停时后台重新生成，暂停时等待继续"
                  disabled={pending || !s.semantic?.configured}
                  onClick={() => setConfirmReset(true)}
                >
                  <RotateCcw size={17} />
                </IconAction>
              </>
            }
          />
        </Suspense>
      </div>
      {confirmReset && (
        <Modal
          id="confirm-reset-semantic"
          title="重置向量索引？"
          description="重置后需要重新调用 Embedding 模型生成向量。"
          onClose={() => {
            if (!pending) setConfirmReset(false);
          }}
        >
          <p className="px-6 py-5 text-sm text-muted leading-7">
            {s.semantic?.paused
              ? '将清空当前向量索引，恢复后台向量化后重新生成。'
              : '将清空当前向量索引，并在后台重新生成。'}
            重建完成前，语义检索暂不可用。
          </p>
          <div className="dialog-footer">
            <Button disabled={pending} onClick={() => setConfirmReset(false)}>
              取消
            </Button>
            <Button
              variant="danger"
              disabled={pending || !s.semantic?.configured}
              onClick={() =>
                run(async () => {
                  await api.post('semantic/rebuild');
                  notify(
                    s.semantic?.paused
                      ? '旧向量已重置，继续后将重新生成。'
                      : '全部向量已重置，后台将重新生成。',
                  );
                  setConfirmReset(false);
                  refresh();
                })
              }
            >
              {pending ? (
                <LoaderCircle size={14} className="animate-spin" />
              ) : (
                <RotateCcw size={14} />
              )}
              {pending ? '正在重置…' : '确认重置'}
            </Button>
          </div>
        </Modal>
      )}
    </Tooltip.Provider>
  );
}
