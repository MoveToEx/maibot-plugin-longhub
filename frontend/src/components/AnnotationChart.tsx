import { Tooltip } from '@base-ui/react/tooltip';
import { BadgeCheck } from 'lucide-react';
import { useMemo } from 'react';
import type { ReactNode } from 'react';
import type { Status } from '../types';
import { ProgressPie } from './ProgressPie';
import { Badge, Panel, PanelHeading } from './ui';

const labels = ['待标注', '已标注未向量化', '已向量化'];
const colors = ['--chart-pending', '--chart-annotated', '--chart-indexed'];
const embeddingLabels = ['待向量化', '已向量化'];
const embeddingColors = ['--chart-pending', '--chart-indexed'];

export function AnnotationChart({ status }: { status: Status }) {
  const counts = status.semantic?.catalog;
  const unannotated = counts?.unannotated ?? status.total;
  const annotated = counts?.annotated_unindexed ?? 0;
  const indexed = counts?.indexed ?? 0;
  const values = useMemo(
    () => [unannotated, annotated, indexed],
    [unannotated, annotated, indexed],
  );
  return (
    <Panel id="annotation-chart" className="annotation-panel">
      <PanelHeading
        title="标注进度"
        action={
          status.semantic?.available && (
            <Tooltip.Provider>
              <Tooltip.Root>
                <Tooltip.Trigger className="semantic-ready" aria-label="语义查询可用" delay={150}>
                  <BadgeCheck size={18} strokeWidth={1.7} />
                </Tooltip.Trigger>
                <Tooltip.Portal>
                  <Tooltip.Positioner side="bottom" align="end" sideOffset={8} className="z-50">
                    <Tooltip.Popup className="tooltip-popup" role="tooltip">
                      语义查询可用。有效标注覆盖率已超过
                      50%，向量索引已就绪。上下文明确相关时优先使用。
                    </Tooltip.Popup>
                  </Tooltip.Positioner>
                </Tooltip.Portal>
              </Tooltip.Root>
            </Tooltip.Provider>
          )
        }
      />
      <ProgressPie values={values} labels={labels} colors={colors} caption="图库标注进度" />
    </Panel>
  );
}

export function EmbeddingProgress({ status, actions }: { status: Status; actions: ReactNode }) {
  const semantic = status.semantic;
  const pending = semantic?.pending ?? 0;
  const indexed = semantic?.indexed ?? 0;
  const values = useMemo(() => [pending, indexed], [pending, indexed]);
  const state = !semantic?.configured
    ? '未配置模型'
    : semantic.error
      ? '索引异常'
      : semantic.paused
        ? '已暂停'
        : pending
          ? '向量化中'
          : indexed
            ? '已完成'
            : '等待标注';
  return (
    <Panel id="embedding-progress" className="task-panel">
      <PanelHeading
        title="Embedding 进度"
        action={
          <Badge
            tone={
              semantic?.error
                ? 'danger'
                : semantic?.paused
                  ? 'neutral'
                  : indexed && !pending
                    ? 'success'
                    : 'neutral'
            }
          >
            {state}
          </Badge>
        }
      />
      <p className="text-xs text-muted mb-4 break-all">
        {semantic?.model || '在连接与设置中选择嵌入模型'}
      </p>
      <ProgressPie
        values={values}
        labels={embeddingLabels}
        colors={embeddingColors}
        caption="Embedding 进度"
        emptyLabel="暂无待向量化标注"
      />
      {semantic?.error && (
        <p className="inline-error" role="alert">
          {semantic.error}
        </p>
      )}
      <div className="task-actions">{actions}</div>
    </Panel>
  );
}
