import { useMutation, useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { api, type Chapter, type ChapterState } from '../api/client';
import { useLiveReady } from '../hooks/useLiveEvents';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { errorMessage } from '../lib/errors';
import { Dialog } from './Dialog';
import { useToast } from './Toast';

interface Props {
  novelId: string;
  /** The selected chapters, in position order. */
  selected: Chapter[];
  /** Leaves select mode (called after a bulk action completes). */
  onDone: () => void;
  /** Export EPUB…: the selection's first and last position; the export dialog is the owner's. */
  onExport: (from: number, to: number) => void;
}

const DESKTOP = '(min-width: 720px)';

const SUMMARY_PARTS: [ChapterState, (n: number) => string][] = [
  ['not_requested', (n) => `${String(n)} not requested`],
  ['pending', (n) => `${String(n)} queued`],
  ['downloading', (n) => `${String(n)} downloading`],
  ['downloaded', (n) => `${String(n)} downloaded`],
  ['error', (n) => `${String(n)} ${n === 1 ? 'error' : 'errors'}`],
];

/** "7 queued, 1 error": the selection's chapters by state. */
function summarise(selected: Chapter[]): string {
  return SUMMARY_PARTS.flatMap(([state, label]) => {
    const n = selected.filter((c) => c.state === state).length;
    return n > 0 ? [label(n)] : [];
  }).join(', ');
}

const button =
  'min-h-11 rounded-lg border border-page/40 px-4 text-sm font-semibold disabled:opacity-40';
const primary =
  'min-h-11 rounded-lg bg-page px-4 text-sm font-semibold text-ink disabled:opacity-40';

/**
 * Action bar of select mode (spec section 10): the selection's range and state summary with
 * Download, Cancel queued, Retry errors and Export EPUB… (phone: Download, Retry, More…, with the
 * rest in a bottom sheet). Bulk actions are one `PATCH /chapters`; the live events update the
 * chapter cache, so nothing is written optimistically.
 */
export function SelectionBar({ novelId, selected, onDone, onExport }: Props) {
  const wide = useMediaQuery(DESKTOP);
  const toast = useToast();
  const [sheetOpen, setSheetOpen] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const exports = useQuery({
    queryKey: ['exports', novelId],
    queryFn: () => api.listExports(novelId),
    enabled: useLiveReady(),
  });
  const patch = useMutation({
    mutationFn: ({ state, chapters }: { state: ChapterState; chapters: Chapter[] }) =>
      api.patchChapters(
        novelId,
        state,
        chapters.map((c) => c.position),
      ),
    onSuccess: (result) => {
      toast.show(
        `Queued ${String(result.updated.length)} · skipped ${String(result.skipped.length)}`,
      );
      onDone();
    },
  });

  const first = selected[0];
  const last = selected[selected.length - 1];
  const queued = selected.filter((c) => c.state === 'pending');
  const failed = selected.filter((c) => c.state === 'error');
  // Waiting exports that cannot finish without the chapters this would dequeue.
  const needed = (exports.data ?? []).filter(
    (e) =>
      e.state === 'waiting' &&
      !e.skip_missing &&
      e.chapter_from !== null &&
      e.chapter_to !== null &&
      queued.some((c) => c.position >= (e.chapter_from ?? 0) && c.position <= (e.chapter_to ?? 0)),
  );
  const none = selected.length === 0;
  const busy = patch.isPending;

  const cancelQueued = () => {
    setSheetOpen(false);
    if (needed.length > 0) setConfirmCancel(true);
    else patch.mutate({ state: 'not_requested', chapters: selected });
  };
  const exportEpub = () => {
    setSheetOpen(false);
    if (first && last) onExport(first.position, last.position);
  };

  const download = (
    <button
      type="button"
      disabled={none || busy}
      onClick={() => {
        patch.mutate({ state: 'pending', chapters: selected });
      }}
      className={primary}
    >
      Download
    </button>
  );
  const retry = (
    <button
      type="button"
      disabled={failed.length === 0 || busy}
      onClick={() => {
        patch.mutate({ state: 'pending', chapters: failed });
      }}
      className={button}
    >
      {wide ? 'Retry errors' : 'Retry'}
    </button>
  );
  const cancel = (
    <button
      type="button"
      disabled={queued.length === 0 || busy}
      onClick={cancelQueued}
      className={button}
    >
      Cancel queued
    </button>
  );
  const exportButton = (
    <button type="button" disabled={none} onClick={exportEpub} className={button}>
      Export EPUB…
    </button>
  );

  const range = first && last ? `${String(first.position)}–${String(last.position)}` : null;
  const summary = summarise(selected);
  const info = (
    <p className="text-sm">
      <strong>{`${String(selected.length)} selected`}</strong>
      {range && ` · ${range}`}
      {summary && ` · ${summary}`}
    </p>
  );

  return (
    <>
      <section
        aria-label="Selection actions"
        className="fixed inset-x-0 bottom-0 z-10 bg-ink p-4 text-page max-[719px]:rounded-t-2xl"
      >
        {patch.isError && (
          <p role="alert" className="mb-2 text-sm font-semibold">
            {errorMessage(patch.error)}
          </p>
        )}
        {wide ? (
          <div className="flex items-center justify-between gap-4">
            {info}
            <div className="flex gap-2">
              {download}
              {cancel}
              {retry}
              {exportButton}
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {info}
            <div className="grid grid-cols-3 gap-2">
              {download}
              {retry}
              <button
                type="button"
                disabled={none}
                onClick={() => {
                  setSheetOpen(true);
                }}
                className={button}
              >
                More…
              </button>
            </div>
          </div>
        )}
      </section>
      {sheetOpen && (
        <Dialog
          title="More actions"
          sheet
          onClose={() => {
            setSheetOpen(false);
          }}
        >
          <div className="mt-4 flex flex-col gap-2 [&>button]:border-line [&>button]:text-ink">
            {cancel}
            {exportButton}
          </div>
        </Dialog>
      )}
      {confirmCancel && (
        <Dialog
          title="Cancel queued chapters?"
          onClose={() => {
            setConfirmCancel(false);
          }}
        >
          <p className="mt-4 text-sm">
            {needed.map((e) => `“${e.filename}”`).join(', ')}{' '}
            {needed.length === 1
              ? 'is a waiting export that needs'
              : 'are waiting exports that need'}{' '}
            some of these chapters. It will stay blocked until they are queued again.
          </p>
          <div className="mt-6 flex justify-end gap-2">
            <button
              type="button"
              data-autofocus
              onClick={() => {
                setConfirmCancel(false);
              }}
              className="min-h-11 rounded-lg border border-line px-4 text-sm font-semibold"
            >
              Keep queued
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmCancel(false);
                patch.mutate({ state: 'not_requested', chapters: selected });
              }}
              className="min-h-11 rounded-lg bg-danger px-4 text-sm font-semibold text-page"
            >
              Cancel queued
            </button>
          </div>
        </Dialog>
      )}
    </>
  );
}
