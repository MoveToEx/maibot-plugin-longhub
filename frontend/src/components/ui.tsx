import { Button as BaseButton } from '@base-ui/react/button';
import { Checkbox } from '@base-ui/react/checkbox';
import { Dialog } from '@base-ui/react/dialog';
import { Select } from '@base-ui/react/select';
import { Switch } from '@base-ui/react/switch';
import {
  AlertCircle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ImageOff,
  LoaderCircle,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { api, imageSource, isMaiBot } from '../api';

export function Button({
  variant = 'secondary',
  className = '',
  type = 'button',
  onClick,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger';
}) {
  return (
    <BaseButton
      type={type}
      className={`button button-${variant} ${className}`}
      {...props}
      onClick={(event) => {
        onClick?.(event);
        if (window.maibot && type === 'submit' && !event.defaultPrevented) {
          // MaiBot forbids native form submission before dispatching submit.
          // Validate locally, then notify React; all I/O uses the authenticated bridge.
          event.preventDefault();
          const form = event.currentTarget.form;
          if (form?.reportValidity())
            form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
        }
      }}
    />
  );
}
export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode;
  tone?: 'neutral' | 'success' | 'accent' | 'danger' | 'warning';
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
export function Panel({ children, className = '', ...props }: React.HTMLAttributes<HTMLElement>) {
  return (
    <section className={`panel ${className}`} {...props}>
      {children}
    </section>
  );
}
export function PanelHeading({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 mb-5">
      <div>
        <h2 className="section-title">{title}</h2>
        {description && <p className="text-muted text-xs mt-1.5 leading-6">{description}</p>}
      </div>
      {action}
    </div>
  );
}
export function SelectField({
  value,
  onChange,
  options,
  label,
  name,
  defaultValue,
  disabled = false,
}: {
  value?: string;
  defaultValue?: string;
  onChange?: (value: string) => void;
  options: { value: string; label: string }[];
  label: string;
  name?: string;
  disabled?: boolean;
}) {
  return (
    <Select.Root<string>
      items={options}
      value={value}
      defaultValue={defaultValue}
      name={name}
      disabled={disabled}
      onValueChange={(value) => {
        if (value !== null) onChange?.(value);
      }}
    >
      <Select.Trigger className="select-trigger" aria-label={label}>
        <Select.Value />
        <Select.Icon>
          <ChevronDown size={14} />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Positioner
          sideOffset={6}
          alignItemWithTrigger={false}
          className="z-50 outline-none"
        >
          <Select.Popup className="select-popup">
            <Select.List>
              {options.map((option) => (
                <Select.Item className="select-item" key={option.value} value={option.value}>
                  <Select.ItemText>{option.label}</Select.ItemText>
                  <Select.ItemIndicator>
                    <Check size={14} />
                  </Select.ItemIndicator>
                </Select.Item>
              ))}
            </Select.List>
          </Select.Popup>
        </Select.Positioner>
      </Select.Portal>
    </Select.Root>
  );
}
export function CheckBox({
  label,
  checked,
  defaultChecked,
  onChange,
  name,
  value,
  indeterminate = false,
}: {
  label: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onChange?: (value: boolean) => void;
  name?: string;
  value?: string;
  indeterminate?: boolean;
}) {
  return (
    <Checkbox.Root
      className="checkbox"
      checked={checked}
      defaultChecked={defaultChecked}
      indeterminate={indeterminate}
      onCheckedChange={onChange}
      aria-label={label}
      name={name}
      value={value}
    >
      <Checkbox.Indicator>
        <Check size={12} strokeWidth={3} />
      </Checkbox.Indicator>
    </Checkbox.Root>
  );
}
export function Toggle({
  label,
  description,
  checked,
  defaultChecked,
  onChange,
  name,
  disabled,
}: {
  label: string;
  description?: string;
  checked?: boolean;
  defaultChecked?: boolean;
  onChange?: (value: boolean) => void;
  name?: string;
  disabled?: boolean;
}) {
  return (
    <label className="flex items-center justify-between gap-6 py-3 cursor-pointer">
      <span className="min-w-0">
        <span className="text-sm font-medium block">{label}</span>
        {description && (
          <span className="text-xs text-muted leading-6 block mt-1">{description}</span>
        )}
      </span>
      <Switch.Root
        className="switch"
        aria-label={label}
        checked={checked}
        defaultChecked={defaultChecked}
        onCheckedChange={onChange}
        name={name}
        disabled={disabled}
      >
        <Switch.Thumb className="switch-thumb" />
      </Switch.Root>
    </label>
  );
}
export function Modal({
  open = true,
  onClose,
  title,
  description,
  children,
  wide = false,
  id,
}: {
  open?: boolean;
  onClose: () => void;
  title: string;
  description: string;
  children: ReactNode;
  wide?: boolean;
  id?: string;
}) {
  return (
    <Dialog.Root
      open={open}
      onOpenChange={(value) => {
        if (!value) onClose();
      }}
    >
      <Dialog.Portal>
        <Dialog.Backdrop className="dialog-backdrop" />
        <Dialog.Viewport className="dialog-viewport">
          <Dialog.Popup className={`dialog-popup ${wide ? 'dialog-wide' : ''}`} id={id}>
            <header className="dialog-header">
              <div className="min-w-0">
                <Dialog.Title className="section-title break-all">{title}</Dialog.Title>
                <Dialog.Description className="text-muted text-xs mt-2 leading-6">
                  {description}
                </Dialog.Description>
              </div>
              <Dialog.Close className="icon-button shrink-0" aria-label="关闭">
                <X size={18} />
              </Dialog.Close>
            </header>
            {children}
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <ImageOff size={25} strokeWidth={1.4} />
      </div>
      <h3 className="font-medium text-sm">{title}</h3>
      {description && <p className="text-xs text-muted max-w-md leading-7 mt-2">{description}</p>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}
export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="error-state" role="alert">
      <AlertCircle size={18} className="shrink-0" />
      <p className="flex-1 break-words">{message}</p>
      <Button onClick={retry}>重试</Button>
    </div>
  );
}
export function Loading({ label = '正在读取' }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted" role="status">
      <LoaderCircle className="animate-spin" size={18} />
      {label}
    </div>
  );
}
export function Pagination({
  page,
  total,
  onChange,
  busy,
}: {
  page: number;
  total: number;
  onChange: (page: number) => void;
  busy?: boolean;
}) {
  const pages = Math.max(1, Math.ceil(total / 30));
  return (
    <nav className="pagination" aria-label="分页">
      <span className="text-xs text-muted">共 {total.toLocaleString()} 条</span>
      <div className="flex items-center gap-3">
        <Button aria-label="上一页" disabled={page <= 1 || busy} onClick={() => onChange(page - 1)}>
          <ChevronLeft size={15} />
        </Button>
        <span className="text-xs tabular-nums">
          {page} / {pages}
        </span>
        <Button
          aria-label="下一页"
          disabled={page >= pages || busy}
          onClick={() => onChange(page + 1)}
        >
          <ChevronRight size={15} />
        </Button>
      </div>
    </nav>
  );
}
export function RemoteImage({
  id,
  url,
  alt,
  className = '',
}: {
  id: string;
  url: string;
  alt: string;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const [preview, setPreview] = useState<string>();
  const holder = useRef<HTMLDivElement>(null);
  const native = isMaiBot();
  const src = native ? preview : imageSource(url);
  useEffect(() => {
    setFailed(false);
    setPreview(undefined);
    if (!native || !holder.current) return;
    let cancelled = false;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        observer.disconnect();
        api
          .get<{ data_url: string }>(`memes/${id}/preview`)
          .then((result) => {
            if (cancelled) return;
            if (/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(result.data_url))
              setPreview(result.data_url);
            else setFailed(true);
          })
          .catch(() => {
            if (!cancelled) setFailed(true);
          });
      },
      { rootMargin: '100px' },
    );
    observer.observe(holder.current);
    return () => {
      cancelled = true;
      observer.disconnect();
    };
  }, [id, url, native]);
  return (
    <div ref={holder} className="remote-image">
      {src && !failed ? (
        <img
          src={src}
          alt={alt}
          loading="lazy"
          referrerPolicy="no-referrer"
          className={className}
          onError={() => setFailed(true)}
        />
      ) : (
        <div className="image-fallback" role="img" aria-label="远程图片暂时不可用">
          <ImageOff size={24} strokeWidth={1.4} />
          <span>{native && !failed ? '正在载入预览' : '图片暂不可用'}</span>
        </div>
      )}
    </div>
  );
}
