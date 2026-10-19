import { useMutation } from '@tanstack/react-query';
import { useId, useState, type SubmitEvent } from 'react';
import { Link } from 'react-router';
import { api, ApiError } from '../api/client';
import { errorMessage } from '../lib/errors';
import { FollowToggle } from './FollowToggle';

function hostsOf(error: ApiError): string[] {
  const hosts = error.details?.hosts;
  if (!Array.isArray(hosts)) return [];
  return hosts.map((h: unknown) =>
    typeof h === 'object' && h !== null && 'host' in h ? String(h.host) : String(h),
  );
}

function ErrorNotice({ error, onClose }: { error: unknown; onClose: () => void }) {
  if (!(error instanceof ApiError)) {
    return <p role="alert">{errorMessage(error)}</p>;
  }
  if (error.code === 'hosts_not_published') {
    return <p role="alert">The worker has not started yet; try again in a minute</p>;
  }
  if (error.code === 'novel_exists') {
    const novelId = error.details?.novel_id;
    return (
      <div role="alert">
        <p>{error.message}</p>
        {typeof novelId === 'string' && (
          <Link to={`/novels/${novelId}`} onClick={onClose} className="underline">
            Open the existing novel
          </Link>
        )}
      </div>
    );
  }
  if (error.code === 'unsupported_host') {
    return (
      <div role="alert">
        <p>{error.message}</p>
        <ul className="mt-1 list-disc pl-5 font-mono text-xs">
          {hostsOf(error).map((host) => (
            <li key={host}>{host}</li>
          ))}
        </ul>
      </div>
    );
  }
  return <p role="alert">{error.message}</p>;
}

/** One-step dialog: table-of-contents URL, follow toggle, Add (spec section 10, screen 2). */
export function AddNovelDialog({ onClose }: { onClose: () => void }) {
  const titleId = useId();
  const inputId = useId();
  const [url, setUrl] = useState('');
  const [followed, setFollowed] = useState(true);
  const create = useMutation({
    mutationFn: () => api.createNovel({ toc_url: url.trim(), followed }),
    onSuccess: onClose,
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-black/50 p-4">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(event) => {
          if (event.key === 'Escape') onClose();
        }}
        className="w-full max-w-lg rounded-2xl bg-surface p-6 text-ink shadow-xl"
      >
        <div className="flex items-center justify-between">
          <h2 id={titleId} className="text-xl font-bold">
            Add novel
          </h2>
          <button type="button" aria-label="Close" onClick={onClose} className="px-2 text-xl">
            ×
          </button>
        </div>
        <form noValidate onSubmit={submit} className="mt-4 flex flex-col gap-4">
          <div className="flex flex-col gap-1">
            <label htmlFor={inputId} className="text-sm font-semibold">
              Table of contents URL
            </label>
            <input
              id={inputId}
              type="url"
              autoFocus
              value={url}
              onChange={(event) => {
                setUrl(event.target.value);
              }}
              className="rounded-lg border border-line bg-page px-3 py-2 font-mono text-sm"
            />
          </div>
          <div className="flex items-center gap-3 text-sm text-ink-muted">
            <FollowToggle followed={followed} onChange={setFollowed} />
            <span>New chapters download automatically and you get a notification.</span>
          </div>
          {create.isError && (
            <div className="text-sm text-[var(--status-error-tab)]">
              <ErrorNotice error={create.error} onClose={onClose} />
            </div>
          )}
          <div className="flex justify-end gap-2 border-t border-line pt-4">
            <button type="button" onClick={onClose} className="px-4 py-2 font-semibold">
              Cancel
            </button>
            <button
              type="submit"
              disabled={url.trim() === '' || create.isPending}
              className="rounded-lg bg-accent px-4 py-2 font-semibold text-accent-ink disabled:opacity-60"
            >
              Add
            </button>
          </div>
          <p className="text-xs text-ink-muted">
            Title, cover and chapters are imported in the background. The novel shows up in your
            library right away, and you can rename it later.
          </p>
        </form>
      </div>
    </div>
  );
}
