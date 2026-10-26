import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import type { Chapter } from '../api/client';
import { ChapterMarkdown } from '../components/ChapterMarkdown';
import { ChapterNotDownloaded } from '../components/ChapterNotDownloaded';
import { Dialog } from '../components/Dialog';
import { ReaderNav } from '../components/ReaderNav';
import { ReaderSettings, ThemeEditorDialog } from '../components/ReaderSettings';
import { ChevronLeftIcon, ChevronRightIcon, HandIcon } from '../components/icons';
import { chapterContentQuery, useChapterContent } from '../hooks/useChapterContent';
import { EXIT_MS, useChapterGestures } from '../hooks/useChapterGestures';
import { useChapters } from '../hooks/useChapters';
import { useBarVisibility } from '../hooks/useBarVisibility';
import { useReadingPosition } from '../hooks/useReadingPosition';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { useReaderSettings, type FontSize, type Width } from '../hooks/useReaderSettings';
import { errorMessage } from '../lib/errors';
import { themeStyle } from '../lib/readerThemes';
import type { GestureView } from '../lib/swipeGesture';

const DESKTOP = '(min-width: 720px)';
const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';

const FONT_CLASS: Record<FontSize, string> = {
  small: 'text-base',
  medium: 'text-lg',
  large: 'text-xl',
  xlarge: 'text-2xl',
};
// The line width only limits the column from the desktop breakpoint up; a phone is always full width.
const WIDTH_CLASS: Record<Width, string> = {
  narrow: 'min-[720px]:max-w-xl',
  medium: 'min-[720px]:max-w-2xl',
  wide: 'min-[720px]:max-w-4xl',
};
/** The settings button; the settings open in a modal dialog (a bottom sheet on a phone). */
function SettingsMenu({
  reader,
  wide,
}: {
  reader: ReturnType<typeof useReaderSettings>;
  wide: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [creatingTheme, setCreatingTheme] = useState(false);
  return (
    <>
      <button
        type="button"
        aria-label="Reading settings"
        aria-haspopup="dialog"
        onClick={() => {
          setOpen(true);
        }}
        className="flex min-h-11 min-w-11 items-center justify-center rounded-lg border border-line px-3 text-sm font-bold"
      >
        Aa
      </button>
      {open && (
        <Dialog
          title="Reading settings"
          sheet={!wide}
          large={wide}
          onClose={() => {
            setOpen(false);
          }}
        >
          <div className="-mx-1 mt-3 max-h-[70dvh] overflow-y-auto p-1">
            {/* The line width only matters from the desktop breakpoint up. */}
            <ReaderSettings
              reader={reader}
              wide={wide}
              onNewTheme={() => {
                setCreatingTheme(true);
              }}
            />
          </div>
        </Dialog>
      )}
      {creatingTheme && (
        <ThemeEditorDialog
          reader={reader}
          onClose={() => {
            setCreatingTheme(false);
          }}
        />
      )}
    </>
  );
}

function PullIndicator({ pull, which }: { pull: GestureView['pull']; which: 'next' | 'previous' }) {
  if (pull?.direction !== which) return null;
  const word = which === 'next' ? 'next' : 'previous';
  const text =
    pull.edge === 'last'
      ? 'Last chapter'
      : pull.edge === 'first'
        ? 'First chapter'
        : pull.ready
          ? `Release for ${word} chapter`
          : `Pull for ${word} chapter`;
  return (
    <div
      style={{ height: `${String(pull.distance)}px` }}
      className={`flex justify-center overflow-hidden text-sm text-ink-muted ${
        which === 'next' ? 'items-start pt-2' : 'items-end pb-2'
      }`}
    >
      {text}
    </div>
  );
}

/** Room the bubble needs to be fully in: its width plus its margin from the screen edge (px). */
const BUBBLE_ROOM_PX = 48;

/**
 * What a sideways drag uncovers on one side of the page: a small vertical bubble saying where
 * the swipe leads. It is fixed to the screen (so it stays in view however far down the chapter
 * is scrolled) and slides in from the screen edge as the page moves away, so it appears to be
 * revealed with the strip. It fills once a release would turn the chapter, and at the ends of
 * the book it turns into a warning that there is nothing further.
 */
function SideHint({
  side,
  neighbour,
  view,
}: {
  side: 'previous' | 'next';
  neighbour: Chapter | undefined;
  view: GestureView;
}) {
  const left = side === 'previous';
  const active = left ? view.offsetX > 0 : view.offsetX < 0;
  const revealed = active ? Math.abs(view.offsetX) : 0;
  const shift = Math.max(0, BUBBLE_ROOM_PX - revealed);
  const state = !neighbour ? 'end' : view.ready ? 'ready' : 'idle';
  const text = !neighbour
    ? left
      ? 'First chapter'
      : 'Last chapter'
    : left
      ? 'Previous chapter'
      : 'Next chapter';
  const look = {
    idle: 'border border-line bg-surface text-ink',
    ready: 'scale-110 bg-accent text-accent-ink',
    end: 'bg-gold text-[#1f1400]',
  }[state];
  return (
    <div
      data-testid={`hint-${side}`}
      aria-hidden={neighbour ? true : undefined}
      style={{
        opacity: Math.min(1, revealed / 30),
        transform: `translateX(${String(left ? -shift : shift)}px)`,
      }}
      className={`pointer-events-none fixed top-[40dvh] z-10 ${left ? 'left-2' : 'right-2'}`}
    >
      <div
        data-testid={`bubble-${side}`}
        data-state={state}
        className={`flex flex-col items-center gap-2 rounded-full px-2 py-3 shadow transition-transform duration-150 motion-reduce:transition-none ${look}`}
      >
        {state === 'end' ? <HandIcon /> : left ? <ChevronLeftIcon /> : <ChevronRightIcon />}
        <span
          role={state === 'end' ? 'status' : undefined}
          className={`text-xs font-semibold leading-none [writing-mode:vertical-rl] ${left ? 'rotate-180' : ''}`}
        >
          {state === 'end' && !active ? '' : text}
        </span>
      </div>
    </div>
  );
}

function Gone({ novelId, message }: { novelId: string; message: string }) {
  return (
    <div className="rounded-xl border border-dashed border-line p-8 text-center">
      <p className="mb-4 font-semibold">{message}</p>
      <p className="flex justify-center gap-4">
        <Link to={`/novels/${novelId}`} className="text-accent underline">
          Back to the novel
        </Link>
        <Link to="/" className="text-accent underline">
          Library
        </Link>
      </p>
    </div>
  );
}

/** Whether a key press belongs to a text field, where the arrow keys must keep working. */
function typing(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
  );
}

/**
 * The reading page (`/novels/:id/chapters/:chapterId`): the chapter's text in a comfortable
 * column, Previous and Next in a bar at the bottom edge, settings for text size and line width,
 * and touch gestures to turn chapters. The chapter itself comes from the chapter list (state,
 * position, neighbours, live updates); the content query supplies the text.
 */
export function Reader() {
  const { id = '', chapterId = '' } = useParams();
  const navigate = useNavigate();
  const client = useQueryClient();
  const wide = useMediaQuery(DESKTOP);
  const reducedMotion = useMediaQuery(REDUCED_MOTION);
  const reader = useReaderSettings();
  const { settings, theme, fontStack } = reader;
  const chapters = useChapters(id);
  const list = chapters.data;
  const index = list?.findIndex((c) => c.id === chapterId) ?? -1;
  const chapter = list?.[index];
  const previous = index > 0 ? list?.[index - 1] : undefined;
  const next = index >= 0 ? list?.[index + 1] : undefined;
  const content = useChapterContent(id, chapter);
  const loaded = content.data !== undefined;
  useReadingPosition(chapterId, loaded);
  const barHidden = useBarVisibility(chapterId);

  const go = (target: Chapter | undefined) => {
    if (target) void navigate(`/novels/${id}/chapters/${target.id}`);
  };
  const {
    ref: surfaceRef,
    view,
    dragging,
    exiting,
    entering,
  } = useChapterGestures({
    hasPrevious: previous !== undefined,
    hasNext: next !== undefined,
    reducedMotion,
    onCommit: (commit) => {
      go(commit === 'next' ? next : previous);
    },
  });

  // Once this chapter's text is in, fetch the neighbours' so a swipe lands on text, not a spinner.
  useEffect(() => {
    if (!loaded) return;
    for (const neighbour of [previous, next]) {
      if (neighbour?.state === 'downloaded') {
        // A failed prefetch is not an error: the neighbour loads when it is opened.
        client.query(chapterContentQuery(id, neighbour)).catch(() => undefined);
      }
    }
  }, [client, id, loaded, previous, next]);

  // No pull-to-refresh or scroll chaining while reading: pulling at the ends turns chapters.
  useEffect(() => {
    document.documentElement.classList.add('reader-open');
    return () => {
      document.documentElement.classList.remove('reader-open');
    };
  }, []);

  // The theme recolours the whole page, outside this component's tree too (body, header): it
  // sets the app's own colour tokens on the root and takes them off again on leaving.
  useEffect(() => {
    if (theme === null) return;
    const root = document.documentElement;
    const style = themeStyle(theme);
    for (const [property, value] of Object.entries(style)) root.style.setProperty(property, value);
    return () => {
      for (const property of Object.keys(style)) root.style.removeProperty(property);
    };
  }, [theme]);

  const title = chapter ? (chapter.title ?? `Chapter ${String(chapter.position)}`) : null;
  useEffect(() => {
    if (title === null) return;
    const before = document.title;
    document.title = `${title} · novelhog`;
    return () => {
      document.title = before;
    };
  }, [title]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;
      if (typing(event.target) || document.querySelector('dialog[open]')) return;
      go(event.key === 'ArrowRight' ? next : previous);
    }
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
    };
  });

  if (chapters.isError && list === undefined) {
    return (
      <div className="flex flex-col gap-4">
        <p role="alert" className="text-danger">
          {errorMessage(chapters.error)}
        </p>
        <Gone novelId={id} message="The chapters could not be loaded" />
      </div>
    );
  }
  if (list === undefined) return <p className="text-ink-muted">Loading…</p>;
  if (!chapter || title === null) {
    return <Gone novelId={id} message="This chapter no longer exists" />;
  }

  const motion = !reducedMotion;
  const pageStyle: CSSProperties = {
    transform:
      exiting !== null
        ? `translateX(${exiting === 'next' ? '-100%' : '100%'})`
        : `translateX(${String(view.offsetX)}px)`,
    opacity: exiting !== null ? 0 : 1,
    transition:
      dragging || !motion
        ? 'none'
        : `transform ${String(EXIT_MS)}ms ease-out, opacity ${String(EXIT_MS)}ms ease-out`,
  };
  // Contrast from a third (so it shows at once) to full where a release would turn the chapter.
  const tint =
    exiting !== null
      ? 1
      : view.progress > 0
        ? 0.35 + 0.65 * view.progress
        : entering !== null
          ? 0.6
          : 0;
  const moving = view.offsetX !== 0 || exiting !== null;
  const enterClass =
    entering === 'next'
      ? 'reader-enter-next'
      : entering === 'previous'
        ? 'reader-enter-previous'
        : '';

  let body: ReactNode;
  if (chapter.state !== 'downloaded') {
    body = <ChapterNotDownloaded novelId={id} chapter={chapter} />;
  } else if (content.isError) {
    body = (
      <div className="flex flex-col items-start gap-3 text-base">
        <p role="alert" className="font-semibold text-danger">
          {errorMessage(content.error)}
        </p>
        <button
          type="button"
          onClick={() => {
            void content.refetch();
          }}
          className="min-h-11 rounded-lg border border-line px-4 text-sm font-semibold"
        >
          Try again
        </button>
      </div>
    );
  } else if (content.data === undefined) {
    body = <p className="text-base text-ink-muted">Loading content…</p>;
  } else {
    body = <ChapterMarkdown markdown={content.data.markdown} />;
  }

  return (
    // The root runs edge to edge and up to the app header, so the page that slides (the top row
    // and the text) and the strip it uncovers fill the whole zone between header and bottom bar.
    <div data-testid="reader-root" className="-mx-4 -mt-4 -mb-4 flex flex-1 flex-col">
      <div
        ref={surfaceRef}
        data-testid="reader-surface"
        className="flex flex-1 touch-pan-y flex-col"
      >
        <PullIndicator pull={view.pull} which="previous" />
        <div
          data-testid="reader-underlay"
          className="relative flex flex-1 flex-col overflow-x-clip"
        >
          {/* The strip under the page gets more contrasting as the drag nears a turn. */}
          <div
            data-testid="reader-tint"
            aria-hidden="true"
            style={{ opacity: tint }}
            className="pointer-events-none absolute inset-0 bg-[color-mix(in_srgb,var(--ink)_16%,var(--page))]"
          />
          <SideHint side="previous" neighbour={previous} view={view} />
          <SideHint side="next" neighbour={next} view={view} />
          <div
            key={chapter.id}
            data-testid="reader-page"
            style={pageStyle}
            className={`relative flex flex-1 flex-col gap-4 bg-page px-4 pt-4 ${moving ? 'shadow-[0_0_14px_color-mix(in_srgb,var(--ink)_25%,transparent)]' : ''} ${enterClass}`}
          >
            <header className="flex items-center justify-between gap-2">
              <Link
                to={`/novels/${id}`}
                className="flex min-h-11 items-center gap-1 text-sm font-semibold text-accent"
              >
                <ChevronLeftIcon />
                Back to novel
              </Link>
              <SettingsMenu reader={reader} wide={wide} />
            </header>
            <article
              style={{ fontFamily: fontStack }}
              className={`mx-auto w-full flex-1 leading-[1.7] [overflow-wrap:anywhere] ${FONT_CLASS[settings.fontSize]} ${WIDTH_CLASS[settings.width]}`}
            >
              <p className="text-sm text-ink-muted">{`${String(chapter.position)} of ${String(list.length)}`}</p>
              <h1 className="mb-6 text-2xl font-bold leading-tight">{title}</h1>
              {body}
            </article>
          </div>
        </div>
        <PullIndicator pull={view.pull} which="next" />
      </div>
      <ReaderNav
        chapters={list}
        index={index}
        hidden={barHidden && !wide}
        wide={wide}
        onPrevious={() => {
          go(previous);
        }}
        onNext={() => {
          go(next);
        }}
      />
    </div>
  );
}
