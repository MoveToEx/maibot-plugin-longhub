import type { Bridge } from './types';

function bridge(): Bridge {
  const value = window.AstrBotPluginView ?? window.AstrBotPluginPage;
  if (!value) throw new Error('请从 AstrBot 插件详情中的「页面」打开 LONG Hub 管理面板。');
  return value;
}

export const api = {
  ready: () => bridge().ready(),
  get: <T>(path: string, params?: Record<string, string | number>) =>
    bridge().apiGet<T>(path, params),
  post: <T>(path: string, body: unknown = {}) => bridge().apiPost<T>(path, body),
};
export const jobLabels: Record<string, string> = {
  idle: '空闲',
  queued: '排队中',
  running: '处理中',
  done: '已完成',
  failed: '失败',
  cancelled: '已取消',
};
export const formatDate = (value?: number | null) =>
  value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '尚无记录';
export const splitTags = (value: string) => [
  ...new Set(
    value
      .split(/[,，\n]/)
      .map((x) => x.trim())
      .filter(Boolean),
  ),
];
export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export function imageSource(value: string): string | undefined {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:' && !url.username && !url.password) return url.href;
  } catch {
    /* Invalid upstream media stays a text placeholder. */
  }
  return undefined;
}
