import { useMutation } from '@tanstack/react-query';
import { Link } from 'react-router';
import { api, type Novel } from '../api/client';
import { errorMessage } from '../lib/errors';
import { relative } from '../lib/time';
import { FollowToggle } from './FollowToggle';
import { useToast } from './Toast';

function initials(name: string): string {
  const words = name.split(/[\s.\-_]+/).filter(Boolean);
  const letters = words.length > 1 ? words.slice(0, 2).map((w) => w[0]) : [name.slice(0, 2)];
  return letters.join('').toUpperCase();
}

function Cover({ novel, name }: { novel: Novel; name: string }) {
  if (novel.cover_url) {
    return <img src={novel.cover_url} alt="" className="h-24 w-16 shrink-0 rounded object-cover" />;
  }
  return (
    <div
      data-testid="cover-placeholder"
      aria-hidden="true"
      className="flex h-24 w-16 shrink-0 items-end rounded bg-accent p-1.5 text-sm font-bold text-accent-ink"
    >
      {initials(name)}
    </div>
  );
}

function pct(count: number, total: number): string {
  return `${String((count / total) * 100)}%`;
}

function ProgressBar({ novel }: { novel: Novel }) {
  const { total_pages: total, downloaded_pages, pending_pages, error_pages } = novel;
  const label = `${String(downloaded_pages)} of ${String(total)} downloaded, ${String(pending_pages)} pending, ${String(error_pages)} errors`;
  return (
    <div
      role="img"
      aria-label={label}
      className="flex h-2 overflow-hidden rounded-full bg-[var(--status-not-requested-tab)]"
    >
      {total > 0 && (
        <>
          <div style={{ width: pct(downloaded_pages, total) }} className="bg-accent" />
          <div style={{ width: pct(pending_pages, total) }} className="bg-accent/40" />
          <div
            style={{ width: pct(error_pages, total) }}
            className="bg-[var(--status-error-tab)]"
          />
        </>
      )}
    </div>
  );
}

function activity(novel: Novel): string | null {
  if (novel.importing) return 'Importing…';
  if (novel.syncing) return 'Syncing…';
  if (novel.pending_pages > 0) return 'Downloading…';
  return null;
}

/** A library card: cover, title, host, progress, follow toggle (spec section 10, screen 1). */
export function NovelCard({ novel }: { novel: Novel }) {
  const toast = useToast();
  const follow = useMutation({
    mutationFn: (followed: boolean) => api.patchNovel(novel.id, { followed }),
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });
  const retry = useMutation({
    mutationFn: () => api.syncNovel(novel.id),
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });

  const name = novel.title ?? novel.host;
  const { total_pages: total, downloaded_pages: downloaded, error_pages: errors } = novel;
  const counts =
    total === 0
      ? 'no chapters yet'
      : `${String(downloaded)} / ${String(total)} downloaded${errors > 0 ? ` · ${String(errors)} errors` : ''}`;
  const busy = activity(novel);

  return (
    <article className="relative flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <div className="flex gap-3">
        <Cover novel={novel} name={name} />
        <div className="min-w-0">
          <h2 className="font-semibold text-ink">
            <Link to={`/novels/${novel.id}`} className="after:absolute after:inset-0">
              {name}
            </Link>
          </h2>
          <p className="font-mono text-xs text-ink-muted">{novel.host}</p>
          {novel.new_pages > 0 && (
            <span className="mt-1 inline-block rounded-full bg-gold/15 px-2 py-0.5 text-xs font-semibold text-gold">
              {String(novel.new_pages)} new
            </span>
          )}
        </div>
      </div>
      <ProgressBar novel={novel} />
      <div className="flex justify-between gap-2 text-xs text-ink-muted">
        <span>{counts}</span>
        {busy && <span>{busy}</span>}
      </div>
      {novel.import_error !== null && (
        <div className="flex items-center justify-between gap-2 text-sm text-[var(--status-error-tab)]">
          <span>Import failed: {novel.import_error}</span>
          <button
            type="button"
            disabled={retry.isPending}
            onClick={() => {
              retry.mutate();
            }}
            className="relative rounded-lg border border-line px-3 py-1 font-semibold text-ink disabled:opacity-60"
          >
            Retry
          </button>
        </div>
      )}
      <div className="flex items-center justify-between gap-2 border-t border-line pt-3">
        <span className="relative">
          <FollowToggle
            followed={novel.followed}
            disabled={follow.isPending}
            onChange={(followed) => {
              follow.mutate(followed);
            }}
          />
        </span>
        {novel.last_refreshed_at && (
          <span className="text-xs text-ink-muted">
            checked {relative(novel.last_refreshed_at)}
          </span>
        )}
      </div>
    </article>
  );
}
