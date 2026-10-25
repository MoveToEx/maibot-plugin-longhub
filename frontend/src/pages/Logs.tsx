import { ArrowUpRight, RotateCcw, ShieldCheck, Webhook } from 'lucide-react';
import { useState } from 'react';
import { useSearchParams } from 'react-router';
import { api, formatDate } from '../api';
import {
  Badge,
  Button,
  CheckBox,
  EmptyState,
  ErrorState,
  Loading,
  Modal,
  Pagination,
  Panel,
  SelectField,
} from '../components/ui';
import { useApp } from '../context';
import { useAction, useResource } from '../hooks';
import type { PageData, WebhookLog } from '../types';

export function Logs() {
  const [params, setParams] = useSearchParams();
  const [detail, setDetail] = useState<WebhookLog | null>(null);
  const { revision, notify } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  const page = Math.min(1_000_000, Math.max(1, Math.floor(Number(params.get('page'))) || 1));
  const event = params.get('event') || '';
  const failed = params.get('failed') === 'true';
  const logs = useResource(
    () =>
      api.get<PageData<WebhookLog>>('webhook/logs', {
        page,
        limit: 30,
        event,
        failed: String(failed),
      }),
    [page, event, failed, revision],
  );
  function filter(key: string, value: string) {
    const next = new URLSearchParams(params);
    next.delete('page');
    value ? next.set(key, value) : next.delete(key);
    setParams(next);
  }
  return (
    <div className="page-stack">
      <div className="flex flex-wrap justify-between gap-4 items-center">
        <div className="flex items-center gap-5">
          <SelectField
            label="事件筛选"
            value={event}
            onChange={(value) => filter('event', value)}
            options={[
              { value: '', label: '全部事件' },
              ...['create', 'update', 'delete', 'restore'].map((value) => ({
                value,
                label: value,
              })),
            ]}
          />
          <label className="flex items-center gap-2.5 text-xs">
            <CheckBox
              label="只看失败"
              checked={failed}
              onChange={(value) => filter('failed', value ? 'true' : '')}
            />
            只看失败
          </label>
        </div>
        <Button
          disabled={pending}
          onClick={() =>
            run(async () => {
              await api.post('webhook/retry');
              notify('待处理通知已设为立即重试。');
              logs.reload();
            })
          }
        >
          <RotateCcw size={14} />
          重试待处理通知
        </Button>
      </div>
      {logs.error ? (
        <ErrorState message={logs.error} retry={logs.reload} />
      ) : !logs.data ? (
        <Loading />
      ) : (
        <>
          <Panel className="table-panel">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>事件 / 图片</th>
                    <th>接收时间</th>
                    <th>签名校验</th>
                    <th>HTTP</th>
                    <th>处理状态</th>
                    <th>
                      <span className="sr-only">操作</span>
                    </th>
                  </tr>
                </thead>
                <tbody id="log-rows">
                  {logs.data.items.map((log) => (
                    <tr key={log.id}>
                      <td>
                        <div className="flex items-center gap-3">
                          <span className="table-icon">
                            <Webhook size={15} />
                          </span>
                          <div>
                            <span className="text-xs font-medium">{log.event || '未知事件'}</span>
                            <p className="text-muted text-[10px] mt-1 font-mono">
                              #{log.image_id || '—'}
                            </p>
                          </div>
                        </div>
                      </td>
                      <td className="text-muted text-xs whitespace-nowrap">
                        {formatDate(log.received)}
                      </td>
                      <td>
                        <Badge tone={log.signature_valid ? 'success' : 'danger'}>
                          {log.signature_valid && <ShieldCheck size={11} />}
                          {log.signature_valid ? '有效' : '失败'}
                        </Badge>
                      </td>
                      <td>
                        <code className="text-xs">{log.http_status}</code>
                      </td>
                      <td>
                        <Badge
                          tone={
                            log.http_status !== 204
                              ? 'danger'
                              : log.processed
                                ? 'success'
                                : log.processing_error
                                  ? 'warning'
                                  : 'neutral'
                          }
                        >
                          {log.http_status !== 204
                            ? '未接受'
                            : log.processed
                              ? '已更新'
                              : log.processing_error
                                ? `待重试 · ${log.attempts} 次`
                                : '等待刷新'}
                        </Badge>
                      </td>
                      <td>
                        <Button
                          variant="ghost"
                          aria-label={`查看日志 ${log.id}`}
                          onClick={() => setDetail(log)}
                        >
                          详情
                          <ArrowUpRight size={13} />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!logs.data.items.length && (
              <EmptyState
                title={failed || event ? '没有符合条件的调用' : '暂时没有调用记录'}
                description="新的 webhook 请求会显示在这里，可查看验签和后续处理结果。"
              />
            )}
          </Panel>
          <Pagination
            page={page}
            total={logs.data.total}
            busy={logs.loading}
            onChange={(value) => {
              const next = new URLSearchParams(params);
              next.set('page', String(value));
              setParams(next);
            }}
          />
        </>
      )}
      <p className="text-xs text-muted leading-7">
        每次调用独立记录，同一图片的待处理通知可以合并刷新。失败通知按退避策略重试。
      </p>
      {detail && (
        <Modal
          id="log-dialog"
          title="Webhook 调用详情"
          description={`${detail.event || '未知事件'} · #${detail.image_id || '—'}`}
          onClose={() => setDetail(null)}
        >
          <div className="p-6">
            <pre id="log-detail" className="code-block">
              {JSON.stringify(detail, null, 2)}
            </pre>
          </div>
        </Modal>
      )}
    </div>
  );
}
