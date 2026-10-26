import { ArcElement, Chart, PieController, Tooltip } from 'chart.js';
import { useEffect, useRef } from 'react';

Chart.register(PieController, ArcElement, Tooltip);

interface Props {
  values: number[];
  labels: string[];
  colors: string[];
  caption: string;
  emptyLabel?: string;
}

export function ProgressPie({ values, labels, colors, caption, emptyLabel = '暂无表情' }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const total = values.reduce((sum, value) => sum + value, 0);
  useEffect(() => {
    if (!canvas.current) return;
    const chart = new Chart(canvas.current, {
      type: 'pie',
      data: { labels, datasets: [{ data: values, borderWidth: 3, hoverOffset: 5 }] },
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
              label: ({ raw }) =>
                `${Number(raw).toLocaleString()} 张 · ${total ? ((Number(raw) / total) * 100).toFixed(1) : 0}%`,
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
  }, [values, labels, colors, total]);
  return (
    <>
      <div className={`annotation-pie ${total === 0 ? 'annotation-pie-empty' : ''}`}>
        <canvas
          ref={canvas}
          role="img"
          aria-label={`${caption}：${labels.map((label, index) => `${label} ${values[index]} 张`).join('，')}`}
        />
        {total === 0 && <span className="chart-empty">{emptyLabel}</span>}
      </div>
      <ul className="chart-legend" aria-label={`${caption}统计`}>
        {values.map((count, index) => (
          <li key={labels[index]}>
            <span className="chart-dot" style={{ background: `var(${colors[index]})` }} />
            <span>{labels[index]}</span>
            <span className="ml-auto tabular-nums text-ink">{count.toLocaleString()}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
