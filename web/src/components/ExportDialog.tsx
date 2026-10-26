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
  /** Null when no title was detected: the file is "Untitled …", as on the server. */
  novelTitle: string | null;
  /** Shown in place of a missing title. */
  novelHost: string;
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
const DEFAULT_MAX_PER_BOOK = 100;

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

interface Part {
  start: number;
  end: number;
  chapters: Chapter[];
}

/** The range cut into consecutive books of `size` chapters; the last one takes the remainder. */
function splitParts(inRange: Chapter[], size: number): Part[] {
  const parts: Part[] = [];
  for (let offset = 0; offset < inRange.length; offset += size) {
    const slice = inRange.slice(offset, offset + size);
    const first = slice[0];
    const last = slice[slice.length - 1];
    if (first && last) parts.push({ start: first.position, end: last.position, chapters: slice });
  }
  return parts;
}

function parsePosition(text: string): number | null {
  return /^\d+$/.test(text.trim()) ? Number(text) : null;
}

const input = 'min-h-11 w-24 rounded-lg border border-line bg-surface px-3 text-ink';
const label = 'text-sm font-semibold';

/**
 * Export dialog (spec section 10): the range, the states of its chapters, and the file name
 * preview; Skip missing chapters swaps "download first" for "export what is downloaded now".
 * A range longer than Maximum chapters per book is cut into several books, listed in a table;
 * each is its own `POST /exports`, and the exports panel follows them live.
 */
export function ExportDialog({
  novelId,
  novelTitle,
  novelHost,
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
  const [maxText, setMaxText] = useState(String(DEFAULT_MAX_PER_BOOK));

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
  const maxPerBook = parsePosition(maxText);
  const maxProblem =
    maxPerBook === null || maxPerBook < 1 ? 'Maximum chapters per book must be at least 1.' : null;
  const valid =
    problem === null &&
    maxProblem === null &&
    start !== null &&
    end !== null &&
    maxPerBook !== null;

  const inRange = valid ? chapters.filter((c) => c.position >= start && c.position <= end) : [];
  const missing = inRange.filter((c) => c.state !== 'downloaded');
  const failedBefore = missing.filter((c) => c.state === 'error').length;
  const downloaded = inRange.length - missing.length;
  const skipping = skipMissing && missing.length > 0;
  const filename = exportFilename(novelTitle, start ?? from, end ?? to, 'epub');
  const allParts = valid ? splitParts(inRange, maxPerBook) : [];
  // The server refuses a skip-missing book with nothing downloaded, so such books are left out.
  const parts = skipping
    ? allParts.filter((part) => part.chapters.some((c) => c.state === 'downloaded'))
    : allParts;
  const several = allParts.length > 1;

  const create = useMutation({
    mutationFn: async () => {
      for (const part of parts) {
        await api.createExport(novelId, {
          chapters: { start: part.start, end: part.end },
          format: 'epub',
          skip_missing: skipping,
        });
      }
    },
    onSuccess: () => {
      const base = skipping ? 'Exporting…' : 'Export queued';
      toast.show(parts.length > 1 ? `${String(parts.length)} exports queued` : base);
      onSubmitted();
    },
  });

  function submit(event: SyntheticEvent) {
    event.preventDefault();
    if (valid && !create.isPending) create.mutate();
  }

  const buttonText = skipping ? 'Export now' : missing.length > 0 ? 'Download & export' : 'Export';
  const submitDisabled = !valid || parts.length === 0 || create.isPending;

  return (
    <Dialog title="Export" sheet={!wide} onClose={onClose}>
      <form onSubmit={submit} className="mt-1 flex flex-col gap-5">
        <p className="-mt-1 text-sm text-ink-muted">{novelTitle ?? novelHost}</p>

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
          <label htmlFor={`${ids}-max`} className={label}>
            Maximum chapters per book
          </label>
          <input
            id={`${ids}-max`}
            type="number"
            inputMode="numeric"
            min={1}
            value={maxText}
            aria-invalid={maxProblem !== null}
            onChange={(event) => {
              setMaxText(event.target.value);
            }}
            className={input}
          />
          {maxProblem !== null && (
            <p role="alert" className="text-sm font-semibold text-danger">
              {maxProblem}
            </p>
          )}
        </div>

        <div className="flex flex-col gap-2">
          <p className={label}>Format</p>
          <p className="w-fit rounded-xl bg-ink px-4 py-2.5 text-sm font-bold text-page">EPUB</p>
        </div>

        {several ? (
          <div className="flex flex-col gap-2">
            <p className={label}>{`Files (${String(allParts.length)})`}</p>
            <div className="max-h-56 overflow-auto rounded-lg border border-line">
              <table className="w-full text-left text-sm">
                <thead className="sticky top-0 bg-surface text-xs text-ink-muted">
                  <tr>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      File
                    </th>
                    <th scope="col" className="px-3 py-2 font-semibold">
                      Chapters
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {allParts.map((part) => {
                    const left = skipping && !parts.includes(part);
                    return (
                      <tr
                        key={part.start}
                        className={`border-t border-line ${left ? 'text-ink-muted line-through' : ''}`}
                      >
                        <td className="break-all px-3 py-2">
                          {exportFilename(novelTitle, part.start, part.end, 'epub')}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2">
                          {`${String(part.chapters.length)}${left ? ' · nothing downloaded' : ''}`}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="text-xs text-ink-muted">
              Includes the cover. Each file appears under Exports when it&apos;s ready.
            </p>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <p className={label}>File</p>
            <p className="break-all rounded-lg border border-line px-3 py-2.5 text-sm">
              {filename}
            </p>
            <p className="text-xs text-ink-muted">
              Includes the cover. The file appears under Exports when it&apos;s ready.
            </p>
          </div>
        )}

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
            disabled={submitDisabled}
            className="min-h-11 rounded-lg bg-accent px-5 text-sm font-semibold text-accent-ink disabled:opacity-40"
          >
            {buttonText}
          </button>
        </div>
      </form>
    </Dialog>
  );
}
