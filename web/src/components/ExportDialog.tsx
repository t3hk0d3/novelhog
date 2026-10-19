import { useMutation } from '@tanstack/react-query';
import { useId, useState, type SyntheticEvent } from 'react';
import { api, type Chapter, type ChapterState } from '../api/client';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { errorMessage } from '../lib/errors';
import { exportFilename } from '../lib/filename';
import { CheckIcon } from './icons';
import { Dialog } from './Dialog';
import { STATE_LABELS } from './StatusIcon';
import { useToast } from './Toast';

interface Props {
  novelId: string;
  novelTitle: string;
  /** The novel's whole chapter list, in position order. */
  chapters: Chapter[];
  /** The range the inputs start with. */
  from: number;
  to: number;
  /** Cancel, Escape or the close button: the owner unmounts the dialog. */
  onClose: () => void;
  /** The export was created (the toast is shown here); the owner unmounts the dialog. */
  onSubmitted: () => void;
}

const DESKTOP = '(min-width: 720px)';
const MAX_SHOWN_RUNS = 4;

const SEGMENT_COLOR: Record<ChapterState, string> = {
  downloaded: 'var(--accent)',
  pending: 'color-mix(in srgb, var(--accent) 40%, transparent)',
  downloading: 'var(--status-downloading-tab)',
  error: 'var(--status-error-tab)',
  not_requested: 'var(--status-not-requested-tab)',
};

interface Run {
  state: ChapterState;
  from: number;
  to: number;
  length: number;
}

function runsOf(chapters: Chapter[]): Run[] {
  const runs: Run[] = [];
  for (const chapter of chapters) {
    const last = runs[runs.length - 1];
    if (last?.state === chapter.state && last.to + 1 === chapter.position) {
      last.to = chapter.position;
      last.length += 1;
    } else {
      runs.push({ state: chapter.state, from: chapter.position, to: chapter.position, length: 1 });
    }
  }
  return runs;
}

function span(from: number, to: number): string {
  return from === to ? String(from) : `${String(from)}–${String(to)}`;
}

/** "601–630, 640", cut after a few ranges. */
function describeMissing(missing: Chapter[]): string {
  const ranges: string[] = [];
  let start: number | undefined;
  let previous: number | undefined;
  for (const { position } of missing) {
    if (start !== undefined && previous !== undefined && position !== previous + 1) {
      ranges.push(span(start, previous));
      start = undefined;
    }
    start ??= position;
    previous = position;
  }
  if (start !== undefined && previous !== undefined) ranges.push(span(start, previous));
  const shown = ranges.slice(0, MAX_SHOWN_RUNS).join(', ');
  return ranges.length > MAX_SHOWN_RUNS ? `${shown}, …` : shown;
}

function barLabel(runs: Run[]): string {
  return `Chapters ${runs
    .slice(0, 12)
    .map((r) => `${span(r.from, r.to)} ${STATE_LABELS[r.state].toLowerCase()}`)
    .join(', ')}`;
}

function parsePosition(text: string): number | null {
  return /^\d+$/.test(text.trim()) ? Number(text) : null;
}

const input = 'min-h-11 w-24 rounded-lg border border-line bg-surface px-3 text-ink';
const label = 'text-sm font-semibold';

/**
 * Export dialog (spec section 10): one export, one file. The range, the states of its chapters,
 * and the file name preview; Skip missing chapters swaps "download first" for "export what is
 * downloaded now". Creating the export is `POST /exports`; the exports panel follows it live.
 */
export function ExportDialog({
  novelId,
  novelTitle,
  chapters,
  from,
  to,
  onClose,
  onSubmitted,
}: Props) {
  const wide = useMediaQuery(DESKTOP);
  const toast = useToast();
  const ids = useId();
  const [fromText, setFromText] = useState(String(from));
  const [toText, setToText] = useState(String(to));
  const [skipMissing, setSkipMissing] = useState(false);

  const last = chapters[chapters.length - 1]?.position ?? 0;
  const start = parsePosition(fromText);
  const end = parsePosition(toText);
  const problem =
    start === null || end === null
      ? 'Enter a chapter number in both fields.'
      : start < 1 || end > last
        ? `Chapters run from 1 to ${String(last)}.`
        : start > end
          ? 'From must not be after To.'
          : null;
  const valid = problem === null && start !== null && end !== null;

  const inRange = valid ? chapters.filter((c) => c.position >= start && c.position <= end) : [];
  const missing = inRange.filter((c) => c.state !== 'downloaded');
  const failedBefore = missing.filter((c) => c.state === 'error').length;
  const downloaded = inRange.length - missing.length;
  const skipping = skipMissing && missing.length > 0;
  const filename = exportFilename(novelTitle, start ?? from, end ?? to, 'epub');

  const create = useMutation({
    mutationFn: () =>
      api.createExport(novelId, {
        chapters: { start: start ?? from, end: end ?? to },
        format: 'epub',
        skip_missing: skipping,
      }),
    onSuccess: () => {
      toast.show(skipping ? 'Exporting…' : 'Export queued');
      onSubmitted();
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (valid && !create.isPending) create.mutate();
  }

  const buttonText = skipping ? 'Export now' : missing.length > 0 ? 'Download & export' : 'Export';

  return (
    <Dialog title="Export" sheet={!wide} onClose={onClose}>
      <form onSubmit={submit} className="mt-1 flex flex-col gap-5">
        <p className="-mt-1 text-sm text-ink-muted">{novelTitle}</p>

        <fieldset className="flex flex-col gap-2">
          <legend className={label}>Chapters</legend>
          <div className="flex flex-wrap items-center gap-3">
            <input
              id={`${ids}-from`}
              aria-label="From"
              type="number"
              inputMode="numeric"
              min={1}
              value={fromText}
              aria-invalid={problem !== null}
              onChange={(event) => {
                setFromText(event.target.value);
              }}
              className={input}
            />
            <span className="text-ink-muted">to</span>
            <input
              id={`${ids}-to`}
              aria-label="To"
              type="number"
              inputMode="numeric"
              min={1}
              value={toText}
              aria-invalid={problem !== null}
              onChange={(event) => {
                setToText(event.target.value);
              }}
              className={input}
            />
            {valid && missing.length === 0 && (
              <p className="flex items-center gap-1.5 text-sm text-ink-muted">
                <CheckIcon />
                {`${String(inRange.length)} ${inRange.length === 1 ? 'chapter' : 'chapters'}, all downloaded`}
              </p>
            )}
          </div>
          {problem !== null && (
            <p role="alert" className="text-sm font-semibold text-danger">
              {problem}
            </p>
          )}
          {valid && (
            <div
              role="img"
              aria-label={barLabel(runsOf(inRange))}
              className="flex h-2 overflow-hidden rounded-full bg-[var(--status-not-requested-tab)]"
            >
              {runsOf(inRange).map((r) => (
                <div
                  key={r.from}
                  style={{
                    flex: `${String(r.length)} 1 0`,
                    minWidth: '2px',
                    background: SEGMENT_COLOR[r.state],
                  }}
                />
              ))}
            </div>
          )}
        </fieldset>

        {valid && missing.length > 0 && (
          <div className="flex flex-col gap-3 rounded-xl bg-[color-mix(in_srgb,var(--accent)_12%,var(--surface))] p-4 text-sm">
            <p>
              <strong>{`${String(missing.length)} ${missing.length === 1 ? 'chapter' : 'chapters'} will be downloaded first`}</strong>
              {` (${describeMissing(missing)}${
                failedBefore > 0 ? `, including ${String(failedBefore)} that failed before` : ''
              }). The export starts as soon as they're all in.`}
            </p>
            <label className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={skipMissing}
                disabled={downloaded === 0}
                onChange={(event) => {
                  setSkipMissing(event.target.checked);
                }}
                className="mt-0.5 size-4"
              />
              <span>
                <span className="font-semibold">Skip missing chapters</span>
                {skipping && (
                  <span className="block text-ink-muted">
                    {`Export the ${String(downloaded)} downloaded chapters now; ${String(missing.length)} are left out`}
                  </span>
                )}
              </span>
            </label>
          </div>
        )}

        <div className="flex flex-col gap-2">
          <p className={label}>Format</p>
          <p className="w-fit rounded-xl bg-ink px-4 py-2.5 text-sm font-bold text-page">EPUB</p>
        </div>

        <div className="flex flex-col gap-2">
          <p className={label}>File</p>
          <p className="break-all rounded-lg border border-line px-3 py-2.5 text-sm">{filename}</p>
          <p className="text-xs text-ink-muted">
            Includes the cover. The file appears under Exports when it&apos;s ready.
          </p>
        </div>

        {create.isError && (
          <p role="alert" className="text-sm font-semibold text-danger">
            {errorMessage(create.error)}
          </p>
        )}

        <div className="flex justify-end gap-2 border-t border-line pt-4 max-[719px]:flex-col-reverse">
          <button
            type="button"
            onClick={onClose}
            className="min-h-11 rounded-lg px-4 text-sm font-semibold"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!valid || create.isPending}
            className="min-h-11 rounded-lg bg-accent px-5 text-sm font-semibold text-accent-ink disabled:opacity-40"
          >
            {buttonText}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
