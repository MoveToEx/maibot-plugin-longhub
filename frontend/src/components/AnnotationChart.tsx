import { Tooltip } from '@base-ui/react/tooltip';
import { ArcElement, Chart, PieController, Tooltip as ChartTooltip } from 'chart.js';
import { BadgeCheck } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { Status } from '../types';
import { Panel, PanelHeading } from './ui';

Chart.register(PieController, ArcElement, ChartTooltip);

const labels = ['待标注', '已标注未向量化', '已向量化'];
const colors = ['--chart-pending', '--chart-annotated', '--chart-indexed'];

export function AnnotationChart({ status }: { status: Status }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const counts = status.semantic?.catalog;
  const unannotated = counts?.unannotated ?? status.total;
  const annotated = counts?.annotated_unindexed ?? 0;
  const indexed = counts?.indexed ?? 0;
  const total = unannotated + annotated + indexed;

  useEffect(() => {
    if (!canvas.current) return;
    const chart = new Chart(canvas.current, {
      type: 'pie',
      data: {
        labels,
        datasets: [{ data: [unannotated, annotated, indexed], borderWidth: 3, hoverOffset: 5 }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        layout: { padding: 6 },
        plugins: {
          legend: { display: false },
          tooltip: {
            displayColors: false,
            padding: 10,
            callbacks: {
              label: ({ raw }) => {
                const count = Number(raw);
                return `${count.toLocaleString()} 张 · ${total ? ((count / total) * 100).toFixed(1) : 0}%`;
              },
            },
          },
        },
      },
    });
    const updateTheme = () => {
      const style = getComputedStyle(document.documentElement);
      chart.data.datasets[0].backgroundColor = colors.map((name) =>
        style.getPropertyValue(name).trim(),
      );
      chart.data.datasets[0].borderColor = style.getPropertyValue('--app-surface').trim();
      const tooltip = chart.options.plugins!.tooltip!;
      tooltip.backgroundColor = style.getPropertyValue('--app-ink').trim();
      tooltip.titleColor = tooltip.bodyColor = style.getPropertyValue('--app-surface').trim();
      chart.update('none');
    };
    updateTheme();
    const observer = new MutationObserver(updateTheme);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => {
      observer.disconnect();
      chart.destroy();
    };
  }, [unannotated, annotated, indexed, total]);

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
                      50%，向量索引已就绪。上下文明确相关时优先使用，随机索图可直接用 SQL。
                    </Tooltip.Popup>
                  </Tooltip.Positioner>
                </Tooltip.Portal>
              </Tooltip.Root>
            </Tooltip.Provider>
          )
        }
      />
      <div className={`annotation-pie ${total === 0 ? 'annotation-pie-empty' : ''}`}>
        <canvas
          ref={canvas}
          role="img"
          aria-label={`图库标注进度：待标注 ${unannotated} 张，已标注未向量化 ${annotated} 张，已向量化 ${indexed} 张`}
        />
        {total === 0 && <span className="chart-empty">暂无表情</span>}
      </div>
      <ul className="chart-legend" aria-label="标注状态统计">
        {[unannotated, annotated, indexed].map((count, index) => (
          <li key={labels[index]}>
            <span className="chart-dot" style={{ background: `var(${colors[index]})` }} />
            <span>{labels[index]}</span>
            <span className="ml-auto tabular-nums text-ink">{count.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </Panel>
  );
}
