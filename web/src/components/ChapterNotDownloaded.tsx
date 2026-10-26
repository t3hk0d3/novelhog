import { useMutation } from '@tanstack/react-query';
import { api, type Chapter, type ChapterState } from '../api/client';
import { errorMessage } from '../lib/errors';
import { DownloadIcon, RetryIcon } from './icons';
import { ICON_COLOR_VAR, STATE_LABELS, StatusGlyph, TAB_COLOR_VAR } from './StatusIcon';

const EXPLANATION: Record<ChapterState, string> = {
  not_requested: 'Download it to read it here.',
  pending: "It's in the download queue and will open here by itself once it's ready.",
  downloading: "It's being downloaded right now and will open here by itself.",
  error: 'The last download failed.',
  downloaded: '',
};

/**
 * What the reader shows for a chapter whose text is not here yet: a large icon of its state, its
 * status as a pill, what happens next, and, where it helps, a centred Download or Retry button.
 * The live events turn it into the text by themselves once the chapter is downloaded.
 */
export function ChapterNotDownloaded({ novelId, chapter }: { novelId: string; chapter: Chapter }) {
  const queue = useMutation({
    mutationFn: () => api.patchChapters(novelId, 'pending', [chapter.position]),
  });
  const { state } = chapter;
  const retry = state === 'error';
  return (
    <section
      aria-label="Chapter status"
      className="flex flex-col items-center gap-4 rounded-2xl border border-dashed border-line p-6 text-center text-base"
    >
      <span
        data-testid="status-badge"
        style={{ background: TAB_COLOR_VAR[state] }}
        className={`flex size-16 items-center justify-center rounded-full ${
          state === 'downloading' ? 'motion-safe:animate-pulse' : ''
        }`}
      >
        <StatusGlyph state={state} size={32} />
      </span>
      <h2 className="text-lg font-bold">This chapter isn&apos;t downloaded yet.</h2>
      <p
        data-testid="status-pill"
        style={{ background: TAB_COLOR_VAR[state], color: ICON_COLOR_VAR[state] }}
        className="inline-flex items-center gap-2 rounded-full px-4 py-1.5 text-sm font-bold"
      >
        <StatusGlyph state={state} size={18} />
        {STATE_LABELS[state]}
      </p>
      <p className="max-w-sm text-ink-muted">{EXPLANATION[state]}</p>
      {retry && chapter.error !== null && (
        <p className="max-w-sm break-words rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {chapter.error}
        </p>
      )}
      {(state === 'not_requested' || retry) && (
        <button
          type="button"
          disabled={queue.isPending}
          onClick={() => {
            queue.mutate();
          }}
          className="inline-flex min-h-11 min-w-48 items-center justify-center gap-2 rounded-lg bg-accent px-5 text-sm font-semibold text-accent-ink disabled:opacity-40"
        >
          {retry ? <RetryIcon /> : <DownloadIcon />}
          {retry ? 'Retry download' : 'Download'}
        </button>
      )}
      {queue.isError && (
        <p role="alert" className="text-sm font-semibold text-danger">
          {errorMessage(queue.error)}
        </p>
      )}
    </section>
  );
}
