import { useMutation, useQuery } from '@tanstack/react-query';
import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import { api, type Chapter, type ChapterState } from '../api/client';
import { useLiveReady } from '../hooks/useLiveEvents';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { errorMessage } from '../lib/errors';
import { exportsNeeding } from '../lib/exportsNeeding';
import { dateOnly, dateTime, relative } from '../lib/time';
import { CancelFailsExportDialog } from './CancelFailsExportDialog';
import { Dialog } from './Dialog';
import { ChevronLeftIcon, ChevronRightIcon, CopyIcon, ExternalLinkIcon } from './icons';
import { StatusIcon, STATE_LABELS } from './StatusIcon';

const DESKTOP = '(min-width: 720px)';
/** A downward drag on the sheet handle this long (px) closes the sheet. */
const SWIPE_CLOSE_PX = 80;

interface Props {
  novelId: string;
  /** The open chapter's id; the chapter itself is looked up in `chapters` so live updates show. */
  chapterId: string;
  /** Every chapter of the novel (the `['chapters', novelId]` cache). */
  chapters: Chapter[];
  /** The rows of the current view, in order; Previous and Next step through them. */
  rows: Chapter[];
  onStep: (chapterId: string) => void;
  onClose: () => void;
}

/** What the primary button does for each state (none while downloading). */
const ACTIONS: Partial<Record<ChapterState, { label: string; next: ChapterState }>> = {
  not_requested: { label: 'Download', next: 'pending' },
  pending: { label: 'Cancel', next: 'not_requested' },
  error: { label: 'Retry download', next: 'pending' },
  downloaded: { label: 'Download again', next: 'pending' },
};

function imagesText(chapter: Chapter): string {
  if (chapter.state !== 'downloaded') return 'not downloaded yet';
  return chapter.has_images ? 'yes' : 'no';
}

const iconButton =
  'flex size-11 items-center justify-center rounded-lg border border-line aria-disabled:opacity-40';

/** The error box: when it failed and the full message with a copy button. */
function ErrorBox({ chapter }: { chapter: Chapter }) {
  const message = useRef<HTMLPreElement>(null);
  const [note, setNote] = useState('');

  function selectText() {
    const node = message.current;
    if (!node) return;
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    setNote('Text selected, copy it');
  }

  function copy() {
    const text = chapter.error ?? '';
    // Insecure contexts have no clipboard: select the text so the user can copy it themselves.
    const clipboard = navigator.clipboard as Clipboard | undefined;
    if (!clipboard) {
      selectText();
      return;
    }
    clipboard.writeText(text).then(() => {
      setNote('Copied');
    }, selectText);
  }

  return (
    <section
      role="group"
      aria-label="Error details"
      className="mt-4 rounded-xl border border-danger/40 bg-danger/10 p-3"
    >
      <div className="flex items-center gap-2 text-danger">
        <StatusIcon state="error" />
        <strong>Error</strong>
        {chapter.error_at !== null && (
          <span className="text-sm text-ink-muted">{`failed ${relative(chapter.error_at)}`}</span>
        )}
      </div>
      <div className="mt-2 flex items-start gap-2">
        <pre
          ref={message}
          className="min-w-0 flex-1 whitespace-pre-wrap break-words rounded-lg border border-danger/30 bg-surface p-2 font-mono text-sm"
        >
          {chapter.error}
        </pre>
        <button
          type="button"
          aria-label="Copy error message"
          onClick={copy}
          className="flex size-9 items-center justify-center rounded-lg border border-danger/40"
        >
          <CopyIcon />
        </button>
      </div>
      <p role="status" className="mt-1 text-xs text-ink-muted">
        {note}
      </p>
    </section>
  );
}

/** Drag handle of the sheet: a downward swipe closes it (Escape and Close still work). */
function SheetHandle({ onClose }: { onClose: () => void }) {
  const startY = useRef<number | null>(null);
  return (
    <div
      data-testid="sheet-handle"
      aria-hidden="true"
      onPointerDown={(event: ReactPointerEvent) => {
        startY.current = event.clientY;
        if (typeof event.currentTarget.setPointerCapture === 'function')
          event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerUp={(event: ReactPointerEvent) => {
        const from = startY.current;
        startY.current = null;
        if (from !== null && event.clientY - from >= SWIPE_CLOSE_PX) onClose();
      }}
      onPointerCancel={() => {
        startY.current = null;
      }}
      className="-mt-3 mb-1 flex h-6 touch-none justify-center"
    >
      <span className="mt-2 h-1 w-10 rounded-full bg-line" />
    </div>
  );
}

/**
 * Chapter details (spec section 10): a modal dialog on desktop and a bottom sheet on phones. The
 * chapter is read from `chapters` by id so live updates refresh it. The primary action is one
 * `PATCH /chapters` with a one-item list; nothing is written optimistically, the live events
 * update the cache.
 */
export function ChapterDetails({ novelId, chapterId, chapters, rows, onStep, onClose }: Props) {
  const wide = useMediaQuery(DESKTOP);
  // Results belong to the chapter they were sent for; stepping away hides them (no remount).
  const [skippedPosition, setSkippedPosition] = useState<number | null>(null);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const exports = useQuery({
    queryKey: ['exports', novelId],
    queryFn: () => api.listExports(novelId),
    enabled: useLiveReady(),
  });
  const patch = useMutation({
    mutationFn: ({ state, position }: { state: ChapterState; position: number }) =>
      api.patchChapters(novelId, state, [position]),
    onMutate: () => {
      setSkippedPosition(null);
    },
    onSuccess: (result, { position }) => {
      setSkippedPosition(result.skipped.length > 0 ? position : null);
    },
  });

  const chapter = chapters.find((c) => c.id === chapterId);
  if (!chapter) return null;
  const mine = patch.variables?.position === chapter.position;
  const skipped = skippedPosition === chapter.position;

  // Step through the view's rows; a chapter outside them (a deep link) steps through all.
  const order = rows.some((c) => c.id === chapterId) ? rows : chapters;
  const index = order.findIndex((c) => c.id === chapterId);
  const previous = order[index - 1];
  const next = order[index + 1];
  const title = chapter.title ?? `Chapter ${String(chapter.position)}`;
  const action = ACTIONS[chapter.state];
  // Cancelling a queued chapter fails the waiting exports that need it (spec section 5.5).
  const needed =
    chapter.state === 'pending' ? exportsNeeding(exports.data, [chapter.position]) : [];

  const field = (label: string, value: React.ReactNode) => (
    <div className="flex gap-4">
      <dt className="w-28 shrink-0 text-ink-muted">{label}</dt>
      <dd className="min-w-0 flex-1 break-words">{value}</dd>
    </div>
  );

  return (
    <>
      <Dialog
        title={`Chapter ${String(chapter.position)} of ${String(chapters.length)}`}
        sheet={!wide}
        onClose={onClose}
      >
        {!wide && <SheetHandle onClose={onClose} />}
        <div className="mt-2 flex items-start justify-between gap-3">
          <h3 className="min-w-0 text-lg font-semibold break-words">{title}</h3>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              aria-label="Previous chapter"
              aria-disabled={previous === undefined}
              onClick={() => {
                if (previous) onStep(previous.id);
              }}
              className={iconButton}
            >
              <ChevronLeftIcon />
            </button>
            <button
              type="button"
              aria-label="Next chapter"
              aria-disabled={next === undefined}
              onClick={() => {
                if (next) onStep(next.id);
              }}
              className={iconButton}
            >
              <ChevronRightIcon />
            </button>
          </div>
        </div>
        {chapter.state === 'error' && <ErrorBox chapter={chapter} />}
        <dl className="mt-4 flex flex-col gap-2 text-sm">
          {field(
            'Status',
            <span
              className={`inline-flex items-center gap-2 ${chapter.state === 'error' ? 'text-danger' : ''}`}
            >
              <StatusIcon state={chapter.state} />
              <span aria-hidden="true">{STATE_LABELS[chapter.state]}</span>
            </span>,
          )}
          {field(
            'Source',
            <a
              href={chapter.url}
              target="_blank"
              rel="noopener noreferrer"
              className="break-all font-mono text-accent underline"
            >
              {chapter.url.replace(/^https?:\/\//, '')}
            </a>,
          )}
          {field('Added', chapter.released_at === null ? '—' : dateOnly(chapter.released_at))}
          {chapter.state === 'pending' &&
            chapter.queued_at !== null &&
            field('Queued', <span className="text-gold-ink">{dateTime(chapter.queued_at)}</span>)}
          {field('Last change', dateTime(chapter.updated_at))}
          {field('Images', <span className="text-ink-muted">{imagesText(chapter)}</span>)}
        </dl>
        {patch.isError && mine && (
          <p role="alert" className="mt-3 text-sm font-semibold text-danger">
            {errorMessage(patch.error)}
          </p>
        )}
        {skipped && (
          <p role="alert" className="mt-3 text-sm font-semibold text-danger">
            The chapter changed before this applied; nothing was changed.
          </p>
        )}
        <div className="mt-4 flex gap-2 border-t border-line pt-4 max-[719px]:[&>*]:flex-1 min-[720px]:justify-end">
          <a
            href={chapter.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex min-h-11 items-center justify-center gap-2 rounded-lg border border-line px-4 text-sm font-semibold"
          >
            <ExternalLinkIcon />
            Open on site
          </a>
          {action && (
            <button
              type="button"
              disabled={patch.isPending && mine}
              onClick={() => {
                if (needed.length > 0) setConfirmCancel(true);
                else patch.mutate({ state: action.next, position: chapter.position });
              }}
              className={`min-h-11 rounded-lg px-4 text-sm font-semibold disabled:opacity-40 ${
                chapter.state === 'downloaded' ? 'border border-line' : 'bg-accent text-accent-ink'
              }`}
            >
              {action.label}
            </button>
          )}
        </div>
      </Dialog>
      {confirmCancel && needed.length > 0 && (
        <CancelFailsExportDialog
          title="Cancel queued chapter?"
          needed={needed}
          subject="this chapter"
          onKeep={() => {
            setConfirmCancel(false);
          }}
          onConfirm={() => {
            setConfirmCancel(false);
            patch.mutate({ state: 'not_requested', position: chapter.position });
          }}
        />
      )}
    </>
  );
}
