import type { Bridge } from './types';

function bridge(): Bridge {
  const value = window.AstrBotPluginView ?? window.AstrBotPluginPage;
  if (!value) throw new Error('请从宿主的 LONG Hub 插件页面打开管理面板。');
  return value;
}

export const isMaiBot = () => !!window.maibot;
// MaiBot permits at most two active WebUI calls per plugin. Include thumbnails.
let active = 0;
const pending: { start: () => void; background: boolean }[] = [];
async function scheduled<T>(operation: () => Promise<T>, background = false): Promise<T> {
  if (active >= 2) await new Promise<void>((start) => pending.push({ start, background }));
  else active++;
  try {
    return await operation();
  } finally {
    const foreground = pending.findIndex((request) => !request.background);
    const next = pending.splice(foreground < 0 ? 0 : foreground, 1)[0];
    if (next) next.start();
    else active--;
  }
}
function theme() {
  document.documentElement.dataset.theme = window.maibot?.context?.theme ?? 'light';
}
window.addEventListener('maibot:context', theme);
export const api = {
  ready: async () => {
    if (!window.maibot) return bridge().ready();
    await window.maibot.ready;
    theme();
  },
  get: <T>(path: string, params?: Record<string, string | number>): Promise<T> => {
    if (!window.maibot) return bridge().apiGet<T>(path, params);
    const query = new URLSearchParams();
    Object.entries(params ?? {}).forEach(([key, value]) => query.set(key, String(value)));
    const internal = '/' + path.replace(/^\/+/, '') + (query.size ? '?' + query : '');
    return scheduled(
      () => window.maibot!.query<T>('read', { path: internal }),
      path.endsWith('/preview'),
    );
  },
  post: <T>(path: string, body: unknown = {}): Promise<T> =>
    window.maibot
      ? scheduled(() =>
          window.maibot!.action<T>('write', {
            path: '/' + path.replace(/^\/+/, ''),
            payload: JSON.stringify(body),
          }),
        )
      : bridge().apiPost<T>(path, body),
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
