import { useMutation, useQuery } from '@tanstack/react-query';
import { api, type Export } from '../api/client';
import { isDeletedNovel } from '../api/events';
import { useLiveReady } from '../hooks/useLiveEvents';
import { errorMessage } from '../lib/errors';
import { dateTime } from '../lib/time';
import { ChevronRightIcon } from './icons';
import { useToast } from './Toast';

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${String(Math.max(1, Math.round(bytes / 1024)))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

const plural = (n: number, one: string, many: string) => `${String(n)} ${n === 1 ? one : many}`;

const action =
  'min-h-11 rounded-lg border border-line px-3 text-sm font-semibold disabled:opacity-40';

function Progress({ downloaded, total }: { downloaded: number; total: number }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      <progress
        value={downloaded}
        max={Math.max(total, 1)}
        aria-label="Chapters downloaded"
        className="h-2 w-32 overflow-hidden rounded-full [&::-moz-progress-bar]:bg-accent [&::-webkit-progress-bar]:bg-[var(--status-not-requested-tab)] [&::-webkit-progress-value]:bg-accent"
      />
      <span>{`${String(downloaded)} / ${String(total)} downloaded`}</span>
    </div>
  );
}

function Spinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-4 rounded-full border-2 border-line border-t-accent motion-safe:animate-spin"
    />
  );
}

function ExportRow({ item }: { item: Export }) {
  const toast = useToast();
  const remove = useMutation({
    mutationFn: () => api.deleteExport(item.novel_id, item.id),
    onSuccess: () => {
      toast.show(item.state === 'waiting' ? 'Export cancelled' : 'Export deleted');
    },
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });
  const queue = useMutation({
    mutationFn: (positions: number[]) => api.patchChapters(item.novel_id, 'pending', positions),
    onSuccess: (result) => {
      toast.show(
        `Queued ${String(result.updated.length)} · skipped ${String(result.skipped.length)}`,
      );
    },
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });
  const again = useMutation({
    mutationFn: () =>
      api.createExport(item.novel_id, {
        chapters: { start: item.chapter_from ?? 0, end: item.chapter_to ?? 0 },
        format: item.format,
        skip_missing: item.skip_missing,
      }),
    onSuccess: () => {
      toast.show('Export queued');
    },
    onError: (error) => {
      toast.show(errorMessage(error));
    },
  });

  const range =
    item.chapter_from !== null && item.chapter_to !== null
      ? `Chapters ${String(item.chapter_from)}–${String(item.chapter_to)}`
      : 'Range changed upstream';
  const busy = remove.isPending || queue.isPending || again.isPending;
  const { errors, not_queued: notQueued } = item.blocked;

  const deleteButton = (label: string) => (
    <button
      type="button"
      disabled={busy}
      onClick={() => {
        remove.mutate();
      }}
      className={action}
    >
      {label}
    </button>
  );

  return (
    <li className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface p-3">
      <div className="flex min-w-0 flex-col gap-1">
        <p className="break-all font-semibold">{item.filename}</p>
        <p className="text-sm text-ink-muted">{range}</p>
        {item.state === 'waiting' && (
          <>
            <Progress downloaded={item.progress.downloaded} total={item.progress.total} />
            {errors.length > 0 && (
              <p className="text-sm font-semibold text-gold-ink">
                {`Blocked by ${plural(errors.length, 'error', 'errors')}`}
              </p>
            )}
            {notQueued.length > 0 && (
              <p className="text-sm font-semibold text-gold-ink">
                {`${plural(notQueued.length, 'chapter', 'chapters')} not queued`}
              </p>
            )}
          </>
        )}
        {item.state === 'building' && (
          <p className="flex items-center gap-2 text-sm">
            <Spinner />
            Building…
          </p>
        )}
        {item.state === 'ready' && (
          <p className="text-sm text-ink-muted">
            {[
              item.size_bytes !== null ? size(item.size_bytes) : null,
              item.ready_at !== null ? dateTime(item.ready_at) : null,
            ]
              .filter((part) => part !== null)
              .join(' · ')}
          </p>
        )}
        {item.state === 'failed' && (
          <p className="text-sm font-semibold text-danger">{item.error ?? 'Export failed'}</p>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        {item.state === 'waiting' && errors.length > 0 && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              queue.mutate(errors);
            }}
            className={action}
          >
            Retry
          </button>
        )}
        {item.state === 'waiting' && notQueued.length > 0 && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              queue.mutate(notQueued);
            }}
            className={action}
          >
            Queue them
          </button>
        )}
        {item.state === 'waiting' && deleteButton('Cancel')}
        {item.state === 'ready' && item.url !== null && (
          <a
            href={item.url}
            download
            className="inline-flex min-h-11 items-center rounded-lg bg-accent px-3 text-sm font-semibold text-accent-ink"
          >
            Download
          </a>
        )}
        {item.state === 'ready' && deleteButton('Delete')}
        {item.state === 'failed' && (
          <button
            type="button"
            disabled={busy || item.chapter_from === null || item.chapter_to === null}
            onClick={() => {
              again.mutate();
            }}
            className={action}
          >
            Try again
          </button>
        )}
        {item.state === 'failed' && deleteButton('Delete')}
      </div>
    </li>
  );
}

/** By first chapter, so lower ranges come first; same start keeps the newest-first API order. */
function byChapters(items: Export[]): Export[] {
  const start = (item: Export) => item.chapter_from ?? Infinity;
  return [...items].sort((a, b) => start(a) - start(b));
}

function summary(items: Export[]): { inProgress: number; ready: number; failed: number } {
  const count = (...states: Export['state'][]) =>
    items.filter((item) => states.includes(item.state)).length;
  return {
    inProgress: count('waiting', 'building'),
    ready: count('ready'),
    failed: count('failed'),
  };
}

/**
 * The novel's exports, lowest chapters first, under a fold whose summary line keeps their state visible
 * (spec section 10). The owner controls `open`: it opens after an export is created and when the
 * page is reached through `#exports`. The cache is kept current by the export events, so actions
 * only call the API. The section is the `#exports` anchor that notification links point to, so
 * it renders even while empty.
 */
export function ExportsPanel({
  novelId,
  open,
  onToggle,
}: {
  novelId: string;
  open: boolean;
  onToggle: (open: boolean) => void;
}) {
  const live = useLiveReady();
  // Subscribes to the novel entry without fetching it: a deleted marker stops the export query.
  const novel = useQuery({
    queryKey: ['novel', novelId],
    queryFn: () => api.getNovel(novelId),
    enabled: false,
  });
  const exports = useQuery({
    queryKey: ['exports', novelId],
    queryFn: () => api.listExports(novelId),
    enabled: live && !isDeletedNovel(novel.data),
  });
  const items = exports.data ?? [];
  const { inProgress, ready, failed } = summary(items);

  return (
    <section id="exports" aria-label="Exports" className="scroll-mt-4">
      {exports.isError && (
        <p role="alert" className="text-danger">
          {errorMessage(exports.error)}
        </p>
      )}
      {items.length > 0 && (
        <details
          open={open}
          className="group"
          onToggle={(event) => {
            onToggle(event.currentTarget.open);
          }}
        >
          <summary className="flex min-h-11 cursor-pointer list-none flex-wrap items-center gap-x-2 text-lg font-bold">
            <span className="text-ink-muted transition-transform motion-reduce:transition-none group-open:rotate-90">
              <ChevronRightIcon />
            </span>
            <h2 className="inline">Exports</h2>
            <span className="text-sm font-semibold text-ink-muted">
              {[
                inProgress > 0 ? `${String(inProgress)} in progress` : null,
                ready > 0 ? `${String(ready)} ready` : null,
              ]
                .filter((part) => part !== null)
                .map((part) => `· ${part}`)
                .join(' ')}
            </span>
            {failed > 0 && (
              <span className="text-sm font-semibold text-danger">{`· ${String(failed)} failed`}</span>
            )}
            <span className="ml-auto text-sm text-ink-muted group-open:hidden">Click to open</span>
          </summary>
          <ul className="mt-2 flex flex-col gap-2">
            {byChapters(items).map((item) => (
              <ExportRow key={item.id} item={item} />
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
