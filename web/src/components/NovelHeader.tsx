import { useMutation } from '@tanstack/react-query';
import { useId, useState, type KeyboardEvent } from 'react';
import { api, type Novel } from '../api/client';
import { errorMessage } from '../lib/errors';
import { relative } from '../lib/time';
import { DeleteNovelDialog } from './DeleteNovelDialog';
import { FollowToggle } from './FollowToggle';
import { useToast } from './Toast';

function initials(name: string): string {
  const words = name.split(/[\s.\-_]+/).filter(Boolean);
  const letters = words.length > 1 ? words.slice(0, 2).map((w) => w[0]) : [name.slice(0, 2)];
  return letters.join('').toUpperCase();
}

function schedule(novel: Novel): string {
  const parts: string[] = [];
  if (novel.last_refreshed_at) parts.push(`checked ${relative(novel.last_refreshed_at)}`);
  const next = new Date(novel.next_refresh_at);
  parts.push(next.getTime() <= Date.now() ? 'next check due' : `next ${relative(next)}`);
  return parts.join(' · ');
}

function counters(novel: Novel): string {
  const parts = [
    `${String(novel.total_pages)} chapters`,
    `${String(novel.downloaded_pages)} downloaded`,
    `${String(novel.pending_pages)} pending`,
  ];
  if (novel.error_pages > 0) {
    parts.push(`${String(novel.error_pages)} ${novel.error_pages === 1 ? 'error' : 'errors'}`);
  }
  return parts.join(' · ');
}

function Title({ novel, name }: { novel: Novel; name: string }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const rename = useMutation({
    mutationFn: (title: string) => api.patchNovel(novel.id, { title }),
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      setEditing(false);
    } else if (event.key === 'Enter') {
      const title = draft.trim();
      if (title !== '' && title !== novel.title) rename.mutate(title);
      setEditing(false);
    }
  }

  if (editing) {
    return (
      <input
        aria-label="Title"
        autoFocus
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
        onKeyDown={onKeyDown}
        onBlur={() => {
          setEditing(false);
        }}
        className="w-full rounded-lg border border-line bg-surface px-2 py-1 text-2xl font-bold"
      />
    );
  }
  return (
    <div className="flex items-center gap-2">
      <h1 className="text-2xl font-bold">{name}</h1>
      <button
        type="button"
        aria-label="Edit title"
        onClick={() => {
          setDraft(novel.title ?? '');
          setEditing(true);
        }}
        className="rounded px-1.5 text-ink-muted"
      >
        ✎
      </button>
    </div>
  );
}

function MoreMenu({
  onExport,
  exportDisabled,
  onDelete,
}: {
  onExport: (() => void) | undefined;
  exportDisabled: boolean;
  onDelete: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div
      className="relative"
      onKeyDown={(event) => {
        if (event.key === 'Escape') setOpen(false);
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen(!open);
        }}
        className="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold"
      >
        More
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 min-w-40 rounded-lg border border-line bg-surface p-1 shadow-lg">
          {onExport && (
            <button
              type="button"
              disabled={exportDisabled}
              onClick={() => {
                setOpen(false);
                onExport();
              }}
              className="block w-full rounded px-3 py-1.5 text-left text-sm disabled:opacity-40"
            >
              Export EPUB…
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onDelete();
            }}
            className="block w-full rounded px-3 py-1.5 text-left text-sm text-danger"
          >
            Delete novel…
          </button>
        </div>
      )}
    </div>
  );
}

/** Cover, editable title, host, TOC link, follow, Sync, schedule, counters, More (screen 3). */
export function NovelHeader({
  novel,
  onExport,
}: {
  novel: Novel;
  /** Opens the export dialog for the whole novel (disabled while it has no chapters). */
  onExport?: () => void;
}) {
  const toast = useToast();
  const [deleting, setDeleting] = useState(false);
  const titleId = useId();
  const name = novel.title ?? novel.host;
  const follow = useMutation({
    mutationFn: (followed: boolean) => api.patchNovel(novel.id, { followed }),
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });
  const sync = useMutation({
    mutationFn: () => api.syncNovel(novel.id),
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });

  return (
    <section aria-labelledby={titleId} className="flex gap-4">
      {novel.cover_url ? (
        <img src={novel.cover_url} alt="" className="h-32 w-20 shrink-0 rounded object-cover" />
      ) : (
        <div
          aria-hidden="true"
          className="flex h-32 w-20 shrink-0 items-end rounded bg-accent p-1.5 text-sm font-bold text-accent-ink"
        >
          {initials(name)}
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div id={titleId}>
          <Title novel={novel} name={name} />
        </div>
        <p className="text-sm text-ink-muted">
          <span className="font-mono">{novel.host}</span> ·{' '}
          <a
            href={novel.toc_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent underline"
          >
            Table of contents
          </a>
        </p>
        <p className="text-sm text-ink-muted">{schedule(novel)}</p>
        <p className="text-sm text-ink-muted">{counters(novel)}</p>
        <div className="flex flex-wrap items-center gap-2">
          <FollowToggle
            followed={novel.followed}
            disabled={follow.isPending}
            describedBy={titleId}
            onChange={(followed) => {
              follow.mutate(followed);
            }}
          />
          <button
            type="button"
            disabled={novel.syncing || sync.isPending}
            onClick={() => {
              sync.mutate();
            }}
            className="rounded-lg border border-line px-3 py-1.5 text-sm font-semibold disabled:opacity-60"
          >
            {novel.syncing ? 'Syncing…' : 'Sync'}
          </button>
          <MoreMenu
            onExport={onExport}
            exportDisabled={novel.total_pages === 0}
            onDelete={() => {
              setDeleting(true);
            }}
          />
        </div>
      </div>
      {deleting && (
        <DeleteNovelDialog
          novel={novel}
          onClose={() => {
            setDeleting(false);
          }}
        />
      )}
    </section>
  );
}
