import type { MaiBotBridge, Status } from './types';

function bridge(): MaiBotBridge {
  const value = window.maibot;
  if (!value) throw new Error('请从 MaiBot 的 LONG Hub 插件页面打开管理面板。');
  return value;
}

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
    await bridge().ready;
    theme();
  },
  get: <T>(path: string, params?: Record<string, string | number>): Promise<T> => {
    const query = new URLSearchParams();
    Object.entries(params ?? {}).forEach(([key, value]) => query.set(key, String(value)));
    const internal = '/' + path.replace(/^\/+/, '') + (query.size ? '?' + query : '');
    return scheduled(
      () => bridge().query<T>('read', { path: internal }),
      path.endsWith('/preview'),
    );
  },
  post: <T>(path: string, body: unknown = {}): Promise<T> =>
    scheduled(() =>
      bridge().action<T>('write', {
        path: '/' + path.replace(/^\/+/, ''),
        payload: JSON.stringify(body),
      }),
    ),
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

export function syncReadiness(status: Status) {
  const configuration = status.configuration;
  const confirmationRequired =
    !!configuration?.app_key_configured &&
    !!configuration.webhook_secret_configured &&
    !configuration.webhook_ready;
  const missing = [];
  if (configuration && !configuration.app_key_configured) missing.push('Appkey');
  if (configuration && !configuration.webhook_secret_configured) missing.push('Webhook 签名密钥');
  const label = status.ready ? '' : confirmationRequired ? '待确认订阅' : '等待配置';
  const message = status.ready
    ? status.receiver.started
      ? ''
      : 'Webhook 接收器未启动，请检查监听地址和端口。'
    : confirmationRequired
      ? '密钥已保存。请先在 LONG Hub 启用 webhook 订阅，再点击「确认订阅并校准」。'
      : missing.length
        ? `请在「连接与设置」配置 ${missing.join('、')}。`
        : '请完成密钥配置并确认 webhook 订阅。';
  return { confirmationRequired, label, message };
}
