import { useMutation } from '@tanstack/react-query';
import { useId, useState, type SubmitEvent } from 'react';
import { Dialog } from './Dialog';
import { Link } from 'react-router';
import { api, ApiError } from '../api/client';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { errorMessage } from '../lib/errors';
import { FollowToggle } from './FollowToggle';

const DESKTOP = '(min-width: 720px)';

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
  const wide = useMediaQuery(DESKTOP);
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
    <Dialog title="Add novel" sheet={!wide} onClose={onClose}>
      <form noValidate onSubmit={submit} className="mt-4 flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <label htmlFor={inputId} className="text-sm font-semibold">
            Table of contents URL
          </label>
          <input
            id={inputId}
            type="url"
            data-autofocus
            value={url}
            onChange={(event) => {
              setUrl(event.target.value);
            }}
            className="min-h-11 rounded-lg border border-line bg-page px-3 py-2 font-mono text-base"
          />
        </div>
        <div className="flex items-center gap-3 text-sm text-ink-muted">
          <FollowToggle followed={followed} onChange={setFollowed} />
          <span>New chapters download automatically and you get a notification.</span>
        </div>
        {create.isError && (
          <div className="text-sm text-danger">
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
    </Dialog>
  );
}
