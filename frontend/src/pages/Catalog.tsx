import {
  ArrowUpRight,
  CheckCheck,
  Search,
  SlidersHorizontal,
  Sparkles,
  Tag,
  X,
} from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, Outlet, useNavigate, useParams, useSearchParams } from 'react-router';
import { api, jobLabels, splitTags } from '../api';
import {
  Badge,
  Button,
  CheckBox,
  EmptyState,
  ErrorState,
  Loading,
  Modal,
  Pagination,
  RemoteImage,
  SelectField,
  Toggle,
} from '../components/ui';
import { useApp } from '../context';
import { useAction, useResource } from '../hooks';
import type { Meme, PageData } from '../types';

export function Catalog() {
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(params.get('q') || '');
  const [selected, setSelected] = useState(new Set<string>());
  const { revision, notify, refresh } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  const q = params.get('q') || '';
  const page = Math.min(1_000_000, Math.max(1, Math.floor(Number(params.get('page'))) || 1));
  const rating = params.get('rating') || '';
  const annotation = params.get('annotation') || '';
  const enabled = params.get('enabled') || '';
  const resource = useResource(
    () => api.get<PageData<Meme>>('memes', { q, page, limit: 30, rating, annotation, enabled }),
    [q, page, rating, annotation, enabled, revision],
  );
  useEffect(() => setQuery(q), [q]);
  function filter(values: Record<string, string>) {
    const next = new URLSearchParams(params);
    next.delete('page');
    Object.entries(values).forEach(([key, value]) =>
      value ? next.set(key, value) : next.delete(key),
    );
    setParams(next);
  }
  function toggle(id: string, checked: boolean) {
    if (checked && selected.size >= 1000) {
      notify('每次最多选择 1000 张表情。', true);
      return;
    }
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }
  const items = resource.data?.items || [];
  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id));
  const hasFilters = !!(q || rating || annotation || enabled);
  return (
    <div className="page-stack">
      <section className="catalog-toolbar" aria-label="目录筛选">
        <form
          className="search-form"
          id="search-form"
          onSubmit={(event) => {
            event.preventDefault();
            filter({ q: query });
          }}
        >
          <Search size={17} className="text-muted shrink-0" />
          <input
            id="query"
            aria-label="搜索目录"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索文字、情绪、场景或标签…"
          />
          <Button variant="primary" type="submit">
            搜索
          </Button>
        </form>
        <div className="filter-row">
          <span className="text-muted flex items-center gap-2 text-xs mr-1">
            <SlidersHorizontal size={14} />
            <span className="hidden sm:inline">筛选</span>
          </span>
          <SelectField
            label="分级筛选"
            value={rating}
            onChange={(value) => filter({ rating: value })}
            options={[
              { value: '', label: '全部分级' },
              ...['none', 'moderate', 'violent'].map((value) => ({ value, label: value })),
            ]}
          />
          <SelectField
            label="标注筛选"
            value={annotation}
            onChange={(value) => filter({ annotation: value })}
            options={[
              { value: '', label: '全部标注' },
              { value: 'missing', label: '缺少有效标注' },
            ]}
          />
          <SelectField
            label="启用状态筛选"
            value={enabled}
            onChange={(value) => filter({ enabled: value })}
            options={[
              { value: '', label: '全部启用状态' },
              { value: 'true', label: '已启用' },
              { value: 'false', label: '已禁用' },
            ]}
          />
          {hasFilters && (
            <Button variant="ghost" onClick={() => setParams({})}>
              <X size={13} />
              清除筛选
            </Button>
          )}
        </div>
      </section>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <CheckBox
            label="选择本页全部表情"
            checked={allSelected}
            indeterminate={!allSelected && items.some((item) => selected.has(item.id))}
            onChange={(checked) => {
              if (checked && new Set([...selected, ...items.map((item) => item.id)]).size > 1000) {
                notify('每次最多选择 1000 张表情。', true);
                return;
              }
              setSelected((current) => {
                const next = new Set(current);
                items.forEach((item) => (checked ? next.add(item.id) : next.delete(item.id)));
                return next;
              });
            }}
          />
          <span className="text-xs text-muted" id="catalog-count">
            {resource.data ? `共 ${resource.data.total.toLocaleString()} 张表情` : '正在读取目录'}
          </span>
          {resource.loading && resource.data && <span className="text-xs text-muted">更新中…</span>}
        </div>
        <div className="flex items-center gap-2">
          {selected.size > 0 && (
            <Button variant="ghost" onClick={() => setSelected(new Set())}>
              清空选择
            </Button>
          )}
          <Button
            id="tag-selected"
            disabled={pending || !selected.size}
            onClick={() =>
              run(async () => {
                const result = await api.post<{ queued: number }>('tagging/enqueue', {
                  ids: [...selected],
                });
                notify(`已加入 ${result.queued} 个标注任务。`);
                setSelected(new Set());
                refresh();
              })
            }
          >
            <Sparkles size={14} />
            标注选中项<span className="count-badge">{selected.size}</span>
          </Button>
        </div>
      </div>
      {resource.error ? (
        <ErrorState message={resource.error} retry={resource.reload} />
      ) : !resource.data ? (
        <Loading />
      ) : items.length ? (
        <>
          <div className="gallery" id="gallery" aria-busy={resource.loading}>
            {items.map((item) => (
              <article
                className={`meme-card ${selected.has(item.id) ? 'selected' : ''} ${!item.enabled ? 'meme-disabled' : ''}`}
                key={item.id}
              >
                <Link
                  to={{ pathname: `/catalog/${item.id}`, search: params.toString() }}
                  className="card-image"
                  aria-label={`查看表情 ${item.id}`}
                >
                  <RemoteImage id={item.id} url={item.image_url} alt={item.text || '无文字表情'} />
                  <span className="card-open">
                    <ArrowUpRight size={16} />
                  </span>
                </Link>
                <div className="card-check">
                  <CheckBox
                    label={`选择表情 ${item.id}`}
                    checked={selected.has(item.id)}
                    onChange={(checked) => toggle(item.id, checked)}
                  />
                </div>
                {!item.enabled && (
                  <span className="card-status">
                    <Badge>已禁用</Badge>
                  </span>
                )}
                <div className="card-body">
                  <h2 className="card-title" title={item.text}>
                    {item.text || '无文字表情'}
                  </h2>
                  <div className="card-tags">
                    {item.effective_tags.slice(0, 3).map((tag) => (
                      <span className="tag" key={tag}>
                        {tag}
                      </span>
                    ))}
                    {!item.effective_tags.length && (
                      <span className="text-[10px] text-muted">等待补充标签</span>
                    )}
                  </div>
                  <div className="card-meta">
                    <span className="truncate" title={item.id}>
                      #{item.id}
                    </span>
                    <span
                      className={`flex items-center gap-1 shrink-0 ${item.annotation_fresh ? 'text-success' : ''}`}
                    >
                      {item.annotation_fresh ? <CheckCheck size={12} /> : <Tag size={11} />}
                      {item.annotation_fresh ? '已标注' : item.rating}
                    </span>
                  </div>
                </div>
              </article>
            ))}
          </div>
          <Pagination
            page={page}
            total={resource.data.total}
            busy={resource.loading}
            onChange={(value) => {
              const next = new URLSearchParams(params);
              next.set('page', String(value));
              setParams(next);
              window.scrollTo({ top: 0, behavior: 'smooth' });
            }}
          />
        </>
      ) : (
        <div className="panel">
          <EmptyState
            title={
              hasFilters ? '没有找到合适的表情' : page > 1 ? '本页暂无表情' : '目录还在等第一张表情'
            }
            description={
              hasFilters
                ? '试试更短的关键词，或放宽筛选条件。'
                : page > 1
                  ? '目录可能已经更新，返回第一页继续浏览。'
                  : '在连接与设置中完成配置，再启动一次全量校准。'
            }
            action={
              hasFilters ? (
                <Button onClick={() => setParams({})}>清除筛选</Button>
              ) : page > 1 ? (
                <Button onClick={() => setParams({})}>返回第一页</Button>
              ) : (
                <Link className="button button-primary" to="/settings">
                  配置连接
                  <ArrowRightIcon />
                </Link>
              )
            }
          />
        </div>
      )}
      <Outlet />
    </div>
  );
}
function ArrowRightIcon() {
  return <ArrowUpRight size={14} />;
}

export function MemeDetail() {
  const { id } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const item = useResource(() => api.get<Meme>(`memes/${id}`), [id]);
  const close = () =>
    navigate({ pathname: '/catalog', search: params.toString() }, { replace: true });
  return (
    <Modal
      id="meme-dialog"
      wide
      title={`表情 #${id}`}
      description="补充你的理解，让模型更准确地使用这张表情。"
      onClose={close}
    >
      {item.error ? (
        <div className="p-6">
          <ErrorState message={item.error} retry={item.reload} />
        </div>
      ) : !item.data ? (
        <Loading />
      ) : (
        <EditMeme key={item.data.id} item={item.data} close={close} />
      )}
    </Modal>
  );
}
function EditMeme({ item, close }: { item: Meme; close: () => void }) {
  const [manual, setManual] = useState(item.manual_tags.join(', '));
  const [excluded, setExcluded] = useState(item.excluded_tags.join(', '));
  const [description, setDescription] = useState(item.manual_description ?? item.ai_description);
  const [useAi, setUseAi] = useState(item.manual_description === null);
  const [enabled, setEnabled] = useState(item.enabled);
  const [queued, setQueued] = useState(false);
  const { refresh, notify } = useApp();
  const { run, pending } = useAction((message) => notify(message, true));
  const excludedSet = new Set(splitTags(excluded));
  return (
    <form
      id="edit-form"
      onSubmit={(event) => {
        event.preventDefault();
        run(async () => {
          await api.post(`memes/${item.id}/edit`, {
            manual_tags: splitTags(manual),
            excluded_tags: splitTags(excluded),
            description: useAi ? null : description,
            enabled,
          });
          notify('人工修订已保存，后续同步会保留。');
          refresh();
          close();
        });
      }}
    >
      <div className="meme-detail">
        <div className="detail-preview">
          <div className="detail-image">
            <RemoteImage id={item.id} url={item.image_url} alt={item.text || '表情预览'} />
          </div>
          <div className="p-5">
            <p className="text-sm leading-7 break-words">{item.text || '无文字表情'}</p>
            <div className="flex gap-2 mt-3">
              <Badge>{item.rating}</Badge>
              <Badge tone={item.annotation_fresh ? 'success' : 'warning'}>
                {item.annotation_fresh ? '标注有效' : '缺少有效标注'}
              </Badge>
            </div>
            <p className="text-xs text-muted leading-7 mt-4 break-words">
              {item.description || '尚无视觉描述'}
            </p>
          </div>
        </div>
        <div className="detail-editor">
          <div className="mb-6">
            <p className="field-title mb-4">
              标签来源{' '}
              <span className="text-muted font-normal ml-2 text-[11px]">点击标签可屏蔽或恢复</span>
            </p>
            {(
              [
                ['LONG Hub', item.hub_tags, 'source-hub'],
                ['视觉模型', item.ai_tags, 'source-ai'],
                ['人工修订', item.manual_tags, 'source-manual'],
              ] as const
            ).map(([label, tags, color]) => (
              <div className="mb-3" key={label}>
                <span className="text-[10px] text-muted block mb-2">{label}</span>
                <div className="flex flex-wrap gap-1.5">
                  {tags.length ? (
                    tags.map((tag) => (
                      <button
                        type="button"
                        key={tag}
                        className={`tag ${color} ${excludedSet.has(tag) ? 'tag-excluded' : ''}`}
                        aria-pressed={excludedSet.has(tag)}
                        onClick={() => {
                          const next = new Set(splitTags(excluded));
                          if (next.has(tag)) next.delete(tag);
                          else next.add(tag);
                          setExcluded([...next].join(', '));
                        }}
                      >
                        {tag}
                      </button>
                    ))
                  ) : (
                    <span className="text-xs text-muted">尚无标签</span>
                  )}
                </div>
              </div>
            ))}
          </div>
          <label className="field-label" htmlFor="manual-tags">
            人工标签
            <textarea
              id="manual-tags"
              rows={2}
              value={manual}
              onChange={(event) => setManual(event.target.value)}
              placeholder="用逗号或换行分隔标签"
            />
          </label>
          <label className="field-label" htmlFor="excluded-tags">
            屏蔽标签
            <textarea
              id="excluded-tags"
              rows={2}
              value={excluded}
              onChange={(event) => setExcluded(event.target.value)}
              placeholder="从所有来源的合并结果中移除"
            />
          </label>
          <label className="field-label" htmlFor="manual-description">
            人工描述
            <textarea
              id="manual-description"
              rows={3}
              maxLength={1000}
              disabled={useAi}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="描述画面和适用的对话场景"
            />
          </label>
          <Toggle label="使用模型描述" checked={useAi} onChange={setUseAi} />
          <Toggle
            label="允许模型使用"
            description="关闭后，这张表情会从聊天候选中隐藏。"
            checked={enabled}
            onChange={setEnabled}
          />
          <div className="text-[11px] text-muted mt-3 leading-6" role="status">
            {queued
              ? '已重新加入标注队列'
              : item.tag_job
                ? `标注任务：${jobLabels[item.tag_job.status]}${item.tag_job.error ? ` · ${item.tag_job.error}` : ''}`
                : '尚未进行视觉标注'}
          </div>
        </div>
      </div>
      <div className="dialog-footer">
        <Button
          disabled={pending}
          onClick={() =>
            run(async () => {
              const result = await api.post<{ queued: number }>('tagging/enqueue', {
                ids: [item.id],
                force: true,
              });
              setQueued(true);
              notify(result.queued ? '已重新加入标注队列。' : '该条目已经在队列中。');
              refresh();
            })
          }
        >
          <Sparkles size={14} />
          重新标注
        </Button>
        <div className="flex items-center gap-2">
          <Button onClick={close} disabled={pending}>
            取消
          </Button>
          <Button type="submit" variant="primary" disabled={pending}>
            {pending ? '保存中…' : '保存修订'}
          </Button>
        </div>
      </div>
    </form>
  );
}
