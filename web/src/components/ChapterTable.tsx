import { useRef, type MouseEvent } from 'react';
import { useHref } from 'react-router';
import type { Chapter } from '../api/client';
import { useMediaQuery } from '../hooks/useMediaQuery';
import type { Selection } from '../hooks/useSelection';
import { dateOnly, relative } from '../lib/time';
import { CheckIcon } from './icons';
import { STATE_LABELS } from './StatusIcon';
import { StatusTab } from './StatusTab';

const DESKTOP = '(min-width: 720px)';
const REDUCED_MOTION = '(prefers-reduced-motion: reduce)';
const LONG_PRESS_MS = 500;
/** A press that moves further than this is a scroll, not a long press. */
const LONG_PRESS_SLOP_PX = 10;

interface Props {
  chapters: Chapter[];
  /** From `useChapterView`; decides the "new" dot. */
  isNew: (chapter: Chapter) => boolean;
  onOpenDetails: (chapter: Chapter) => void;
  /** From `useSelection`; without it the table has no select mode. */
  selection?: Selection;
}

/** Drawn rounded-square checkbox: visual only. Slides in unless the user prefers reduced motion. */
function Checkbox({ checked, animate }: { checked: boolean; animate: boolean }) {
  return (
    <span
      aria-hidden="true"
      data-testid="row-checkbox"
      className={`flex w-6 shrink-0 items-center self-center ${
        animate ? 'transition-[width,opacity] duration-200 starting:w-0 starting:opacity-0' : ''
      }`}
    >
      <span
        data-checked={checked}
        className={`flex size-5 items-center justify-center rounded-md border-2 ${
          checked
            ? 'border-accent bg-accent text-accent-ink'
            : 'border-ink-muted bg-surface text-transparent'
        }`}
      >
        {checked && <CheckIcon />}
      </span>
    </span>
  );
}

/** Press-and-hold on a row: fires `onLongPress` once, and the click that follows is swallowed. */
function useLongPress(onLongPress: () => void) {
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const start = useRef<{ x: number; y: number }>(undefined);
  const fired = useRef(false);
  const cancel = () => {
    clearTimeout(timer.current);
    start.current = undefined;
  };
  return {
    handlers: {
      onPointerDown: (event: React.PointerEvent) => {
        if (event.button !== 0) return;
        fired.current = false;
        start.current = { x: event.clientX, y: event.clientY };
        timer.current = setTimeout(() => {
          fired.current = true;
          start.current = undefined;
          onLongPress();
        }, LONG_PRESS_MS);
      },
      onPointerMove: (event: React.PointerEvent) => {
        const from = start.current;
        if (from && Math.hypot(event.clientX - from.x, event.clientY - from.y) > LONG_PRESS_SLOP_PX)
          cancel();
      },
      onPointerUp: cancel,
      onPointerCancel: cancel,
      onPointerLeave: cancel,
      // A touch long press also opens the context menu; the row owns that gesture.
      onContextMenu: (event: MouseEvent) => {
        if (fired.current) event.preventDefault();
      },
    },
    /** True (once) when the click just ended a long press. */
    consumeClick: () => {
      const was = fired.current;
      fired.current = false;
      return was;
    },
  };
}

function Row({
  chapter,
  wide,
  isNew,
  onOpenDetails,
  selection,
}: { chapter: Chapter; wide: boolean } & Omit<Props, 'chapters'>) {
  const title = chapter.title ?? `Chapter ${String(chapter.position)}`;
  const reducedMotion = useMediaQuery(REDUCED_MOTION);
  // In-app target (current route plus ?chapter=<id>) so every way of following the link stays here.
  const href = useHref({ search: `?chapter=${chapter.id}` });
  const selecting = selection?.active === true;
  const selected = selection?.isSelected(chapter.id) === true;
  const press = useLongPress(() => {
    if (!selection) return;
    if (selection.active) selection.rangeTo(chapter.id);
    else selection.enter(chapter.id);
  });

  function onClick(event: MouseEvent) {
    event.preventDefault();
    if (!selection) {
      onOpenDetails(chapter);
      return;
    }
    if (press.consumeClick()) return;
    if (selection.active) {
      if (event.shiftKey) selection.rangeTo(chapter.id);
      else selection.toggle(chapter.id);
    } else if (event.shiftKey || event.ctrlKey || event.metaKey) selection.enter(chapter.id);
    else onOpenDetails(chapter);
  }

  const cells = (
    <>
      {selecting && <Checkbox checked={selected} animate={!reducedMotion} />}
      <span className="w-10 shrink-0 self-center font-semibold text-ink-muted">
        {chapter.position}
      </span>
      <span className="flex min-w-0 flex-1 items-center gap-2 py-2">
        <span className="truncate">{title}</span>
        {isNew(chapter) && (
          <span
            data-testid="new-dot"
            title="New"
            className="size-2 shrink-0 rounded-full bg-gold"
          />
        )}
      </span>
      {wide && (
        <>
          <span className="w-24 shrink-0 self-center text-ink-muted">
            {chapter.released_at === null ? (
              <span title="Part of the initial import">—</span>
            ) : (
              dateOnly(chapter.released_at)
            )}
          </span>
          <span className="w-28 shrink-0 self-center text-ink-muted">
            {relative(chapter.updated_at)}
          </span>
        </>
      )}
      <span aria-hidden="true" className="flex">
        <StatusTab state={chapter.state} chevron={!selecting} />
      </span>
    </>
  );
  const layout = 'flex min-h-11 select-none items-stretch gap-3 ps-3 text-sm min-[720px]:ps-5';

  if (selecting) {
    return (
      <li
        role="option"
        aria-selected={selected}
        aria-label={`${title}, ${STATE_LABELS[chapter.state]}`}
        tabIndex={0}
        onClick={onClick}
        onKeyDown={(event) => {
          if (event.key !== ' ' && event.key !== 'Enter') return;
          event.preventDefault();
          selection.toggle(chapter.id);
        }}
        {...press.handlers}
        className={`${layout} cursor-pointer border-b border-line ${selected ? 'bg-accent/15' : 'hover:bg-page'}`}
      >
        {cells}
      </li>
    );
  }
  return (
    <li className="border-b border-line">
      <a
        href={href}
        aria-label={`${title}, ${STATE_LABELS[chapter.state]}, open details`}
        onClick={onClick}
        {...press.handlers}
        className={`${layout} hover:bg-page`}
      >
        <span aria-hidden="true" className="contents">
          {cells}
        </span>
      </a>
    </li>
  );
}

/**
 * Chapter table (spec section 10). Normal mode: rows are links that open chapter details. With a
 * `selection` in select mode: rows form a multi-select list and a click toggles. Never shows errors.
 */
export function ChapterTable({ chapters, isNew, onOpenDetails, selection }: Props) {
  const wide = useMediaQuery(DESKTOP);
  const reducedMotion = useMediaQuery(REDUCED_MOTION);
  const selecting = selection?.active === true;
  return (
    <div>
      {wide && (
        <div className="flex items-center gap-3 border-b border-line bg-page py-2 ps-5 text-xs font-semibold uppercase tracking-wide text-ink-muted">
          {selecting && (
            <span
              className={`flex w-6 shrink-0 ${
                reducedMotion
                  ? ''
                  : 'transition-[width,opacity] duration-200 starting:w-0 starting:opacity-0'
              }`}
            >
              <button
                type="button"
                role="checkbox"
                aria-checked={
                  selection.visibleState === 'all'
                    ? true
                    : selection.visibleState === 'some'
                      ? 'mixed'
                      : false
                }
                aria-label="Select all visible chapters"
                data-state={selection.visibleState}
                onClick={selection.toggleAllVisible}
                className={`flex size-5 items-center justify-center rounded-md border-2 ${
                  selection.visibleState === 'none'
                    ? 'border-ink-muted bg-surface'
                    : 'border-accent bg-accent text-accent-ink'
                }`}
              >
                {selection.visibleState === 'all' && <CheckIcon />}
                {selection.visibleState === 'some' && (
                  <span aria-hidden="true" className="h-0.5 w-2.5 rounded bg-accent-ink" />
                )}
              </button>
            </span>
          )}
          <span aria-hidden="true" className="w-10 shrink-0">
            #
          </span>
          <span aria-hidden="true" className="flex-1">
            Title
          </span>
          <span aria-hidden="true" className="w-24 shrink-0">
            Added
          </span>
          <span aria-hidden="true" className="w-28 shrink-0">
            Updated
          </span>
          <span className="w-12 shrink-0" />
        </div>
      )}
      <ul role={selecting ? 'listbox' : undefined} aria-multiselectable={selecting || undefined}>
        {chapters.map((chapter) => (
          <Row
            key={chapter.id}
            chapter={chapter}
            wide={wide}
            isNew={isNew}
            onOpenDetails={onOpenDetails}
            selection={selection}
          />
        ))}
      </ul>
    </div>
  );
}
