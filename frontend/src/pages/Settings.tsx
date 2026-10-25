import {
  ArrowUpRight,
  Check,
  Copy,
  Eye,
  EyeOff,
  KeyRound,
  Network,
  Save,
  SlidersHorizontal,
  Sparkles,
} from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { api, splitTags } from '../api';
import {
  Badge,
  Button,
  CheckBox,
  ErrorState,
  Loading,
  Panel,
  SelectField,
  Toggle,
} from '../components/ui';
import { useApp } from '../context';
import { useAction, useResource } from '../hooks';
import type { SettingsData } from '../types';

const TEMPLATE = '{"event":{{json .Event}},"imageId":{{json .Data.ImageID}}}';
function SettingsSection({
  title,
  description,
  icon,
  children,
  action,
}: {
  title: string;
  description: string;
  icon: ReactNode;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <Panel>
      <div className="settings-section-heading">
        <span className="settings-icon">{icon}</span>
        <div className="flex-1">
          <h2 className="section-title">{title}</h2>
          <p className="text-xs text-muted mt-1.5 leading-6">{description}</p>
        </div>
        {action}
      </div>
      {children}
    </Panel>
  );
}
function Field({
  name,
  title,
  note,
  type = 'text',
  value,
  min,
  max,
  placeholder,
  required = true,
}: {
  name: string;
  title: string;
  note?: string;
  type?: string;
  value?: string | number;
  min?: number;
  max?: number;
  placeholder?: string;
  required?: boolean;
}) {
  return (
    <label className="field-label" htmlFor={`setting-${name}`}>
      {title}
      <input
        id={`setting-${name}`}
        name={name}
        type={type}
        defaultValue={value}
        min={min}
        max={max}
        placeholder={placeholder}
        required={required}
      />
      {note && <span className="field-note">{note}</span>}
    </label>
  );
}
function SecretField({
  name,
  title,
  configured,
}: {
  name: string;
  title: string;
  configured: boolean;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <label className="field-label" htmlFor={`setting-${name}`}>
      <span className="flex items-center justify-between">
        {title}
        <Badge tone={configured ? 'success' : 'warning'}>{configured ? '已配置' : '未配置'}</Badge>
      </span>
      <div className="secret-field">
        <input
          id={`setting-${name}`}
          type={visible ? 'text' : 'password'}
          name={name}
          autoComplete="new-password"
          placeholder="留空保留已有值"
        />
        <Button
          variant="ghost"
          aria-label={visible ? `隐藏${title}` : `显示${title}`}
          onClick={() => setVisible((value) => !value)}
        >
          {visible ? <EyeOff size={15} /> : <Eye size={15} />}
        </Button>
      </div>
      <span className="field-note">密钥不会回传到页面，输入新值时才会替换。</span>
    </label>
  );
}
export function Settings() {
  const resource = useResource(() => api.get<SettingsData>('settings'), []);
  if (resource.error) return <ErrorState message={resource.error} retry={resource.reload} />;
  if (!resource.data || resource.loading) return <Loading />;
  return <SettingsForm key={resource.version} data={resource.data} reload={resource.reload} />;
}
function SettingsForm({ data, reload }: { data: SettingsData; reload: () => void }) {
  const { settings: s, providers } = data;
  const { notify, refresh } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  const [copied, setCopied] = useState(false);
  const options = [
    { value: '', label: '暂不启用视觉标注' },
    ...providers.map((provider) => ({ value: provider.id, label: provider.name })),
  ];
  if (s.vision_provider_id && !providers.some((provider) => provider.id === s.vision_provider_id))
    options.push({ value: s.vision_provider_id, label: `${s.vision_provider_id}（当前不可用）` });
  const embeddingProviders = data.embedding_providers ?? [];
  const embeddingOptions = [
    { value: '', label: '暂不启用语义检索' },
    ...embeddingProviders.map((provider) => ({ value: provider.id, label: provider.name })),
  ];
  if (
    s.embedding_provider_id &&
    !embeddingProviders.some((provider) => provider.id === s.embedding_provider_id)
  )
    embeddingOptions.push({
      value: s.embedding_provider_id,
      label: `${s.embedding_provider_id}（当前不可用）`,
    });
  return (
    <form
      id="settings-form"
      className="page-stack settings-form"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const payload: Record<string, unknown> = {};
        const numeric = [
          'webhook_port',
          'sync_interval_hours',
          'tagging_rpm',
          'tagging_concurrency',
          'candidate_limit',
          'max_image_mb',
          'webhook_log_days',
          'webhook_log_limit',
          'embedding_rpm',
        ];
        for (const key of [
          'api_base',
          'app_key',
          'webhook_secret',
          'webhook_host',
          'vision_provider_id',
          'embedding_provider_id',
        ]) {
          const value = String(form.get(key) ?? '').trim();
          if (['app_key', 'webhook_secret'].includes(key) && !value) continue;
          payload[key] = value;
        }
        numeric.forEach((key) => {
          payload[key] = Number(form.get(key));
        });
        payload.webhook_ready = form.has('webhook_ready');
        payload.auto_tagging = form.has('auto_tagging');
        payload.image_hosts = splitTags(String(form.get('image_hosts') ?? ''));
        payload.allowed_ratings = form.getAll('ratings');
        run(async () => {
          await api.post('settings/save', payload);
          notify('配置已保存，后台服务已重新启动。');
          reload();
          refresh();
        });
      }}
    >
      <fieldset disabled={pending} className="contents">
        <SettingsSection
          title="LONG Hub 连接"
          description="连接上游目录，保持表情元数据同步。"
          icon={<KeyRound size={18} />}
          action={
            <a
              className="text-link"
              href="https://docs.longhub.top/api/"
              target="_blank"
              rel="noreferrer"
            >
              API 文档
              <ArrowUpRight size={13} />
            </a>
          }
        >
          <Field title="API 基础地址" name="api_base" type="url" value={s.api_base} />
          <div className="form-grid">
            <SecretField title="Appkey" name="app_key" configured={s.app_key_configured} />
            <SecretField
              title="Webhook 签名密钥"
              name="webhook_secret"
              configured={s.webhook_secret_configured}
            />
          </div>
        </SettingsSection>
        <SettingsSection
          title="Webhook 与校准"
          description="让 LONG Hub 的更新通知抵达你的目录。"
          icon={<Network size={18} />}
        >
          <div className="form-grid">
            <Field
              title="监听地址"
              name="webhook_host"
              value={s.webhook_host}
              note="本机反代用 127.0.0.1，容器映射用 0.0.0.0。"
            />
            <Field
              title="监听端口"
              name="webhook_port"
              type="number"
              value={s.webhook_port}
              min={1}
              max={65535}
            />
            <Field
              title="全量校准间隔（小时）"
              name="sync_interval_hours"
              type="number"
              value={s.sync_interval_hours}
              min={1}
              max={720}
            />
            <Field
              title="图片域名白名单"
              name="image_hosts"
              value={s.image_hosts.join(', ')}
              note="多个域名以逗号分隔，图片仅接受 HTTPS 地址。"
            />
          </div>
          <div className="webhook-template">
            <div className="flex items-center justify-between mb-3 gap-3">
              <span className="text-xs font-medium">Hub webhook 请求体模板</span>
              <Button
                variant="ghost"
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(TEMPLATE);
                    setCopied(true);
                    notify('请求体模板已复制。');
                  } catch {
                    notify('浏览器未允许复制，请选择模板文字后手动复制。', true);
                  }
                }}
              >
                {copied ? <Check size={13} /> : <Copy size={13} />}
                {copied ? '已复制' : '复制'}
              </Button>
            </div>
            <code className="block text-xs text-accent break-all leading-7 select-text">
              {TEMPLATE}
            </code>
            <p className="text-[11px] text-muted mt-3 leading-6">
              公网回调路径 <code>/hooks/longhub</code> · 订阅位掩码 <code>7</code> ·
              签名密钥需与本页一致
            </p>
          </div>
          <Toggle
            label="已在 LONG Hub 启用订阅"
            description="确认上游订阅已启用，保存后会自动启动全量同步。"
            name="webhook_ready"
            defaultChecked={s.webhook_ready}
          />
        </SettingsSection>
        <SettingsSection
          title="模型与聊天"
          description="选择视觉提供商，控制标注用量和聊天候选。"
          icon={<Sparkles size={18} />}
        >
          <div className="form-grid">
            <label className="field-label">
              视觉标注提供商
              <SelectField
                label="视觉标注提供商"
                name="vision_provider_id"
                defaultValue={s.vision_provider_id}
                options={options}
              />
              <span className="field-note">请选择原生支持图片输入的 AstrBot 提供商。</span>
            </label>
            <Field
              title="每分钟最多标注调用"
              name="tagging_rpm"
              type="number"
              value={s.tagging_rpm}
              min={1}
              max={600}
            />
            <Field
              title="标注并发"
              name="tagging_concurrency"
              type="number"
              value={s.tagging_concurrency}
              min={1}
              max={8}
            />
            <Field
              title="自动候选数"
              name="candidate_limit"
              type="number"
              value={s.candidate_limit}
              min={1}
              max={10}
            />
            <Field
              title="单图最大大小（MiB）"
              name="max_image_mb"
              type="number"
              value={s.max_image_mb}
              min={1}
              max={100}
            />
          </div>
          <Toggle
            label="自动标注聊天候选"
            description="缺少有效标注的候选会按原有限速加入后台队列。"
            name="auto_tagging"
            defaultChecked={s.auto_tagging}
          />
          <div className="border-t border-line pt-5 mt-2">
            <p className="field-title mb-4">允许聊天使用的分级</p>
            <div className="flex flex-wrap gap-3">
              {(['none', 'moderate', 'violent'] as const).map((rating) => (
                <label key={rating} className="rating-option">
                  <CheckBox
                    label={`允许 ${rating}`}
                    name="ratings"
                    value={rating}
                    defaultChecked={s.allowed_ratings.includes(rating)}
                  />
                  <span>{rating}</span>
                </label>
              ))}
            </div>
          </div>
        </SettingsSection>
        <SettingsSection
          title="语义检索"
          description="使用 AstrBot 的 Embedding 提供商和内置向量库理解表情含义。"
          icon={<Sparkles size={18} />}
        >
          <div className="form-grid">
            <label className="field-label">
              Embedding 提供商
              <SelectField
                label="Embedding 提供商"
                name="embedding_provider_id"
                defaultValue={s.embedding_provider_id || ''}
                options={embeddingOptions}
              />
              <span className="field-note">
                在 AstrBot 提供商设置中添加嵌入模型，无需在此填写密钥。
              </span>
            </label>
            <Field
              title="每分钟最多 Embedding 请求数"
              name="embedding_rpm"
              type="number"
              value={s.embedding_rpm ?? 30}
              min={1}
              max={600}
              note="后台向量化与概念查询合计限速，每个后台批次最多 16 张。"
            />
          </div>
          <p className="text-xs text-muted leading-7">
            可发送图片的有效标注覆盖率严格超过
            50%，且向量索引同步完成后，模型会优先用概念搜索，再判断是否发送。有效标注包括当前视觉标注与人工描述或标签；过期视觉标注和仅有上游标签的图片不计入覆盖率。
          </p>
          <p className="text-xs text-muted leading-7 mt-2">
            选择后会自动在后台建立索引并调用嵌入模型，可在「标注任务」中查看进度和重建索引。模型、维度或标注发生变化时会自动更新。
          </p>
        </SettingsSection>
        <SettingsSection
          title="日志保留"
          description="按时间和数量清理调用日志，未完成通知独立保留。"
          icon={<SlidersHorizontal size={18} />}
        >
          <div className="form-grid">
            <Field
              title="保留天数"
              name="webhook_log_days"
              type="number"
              min={1}
              max={365}
              value={s.webhook_log_days}
            />
            <Field
              title="最多条数"
              name="webhook_log_limit"
              type="number"
              min={100}
              max={100000}
              value={s.webhook_log_limit}
            />
          </div>
        </SettingsSection>
      </fieldset>
      <div className="save-bar">
        <span className="text-xs text-muted leading-6">
          保存后重启后台服务，本地目录与任务会保留。
        </span>
        <Button variant="primary" type="submit" disabled={pending}>
          <Save size={14} />
          {pending ? '正在保存…' : '保存并应用'}
        </Button>
      </div>
    </form>
  );
}
