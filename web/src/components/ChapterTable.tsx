import type { Chapter } from '../api/client';
import { useMediaQuery } from '../hooks/useMediaQuery';
import { dateOnly, relative } from '../lib/time';
import { STATE_LABELS } from './StatusIcon';
import { StatusTab } from './StatusTab';

const DESKTOP = '(min-width: 720px)';

interface Props {
  chapters: Chapter[];
  /** From `useChapterView`; decides the "new" dot. */
  isNew: (chapter: Chapter) => boolean;
  onOpenDetails: (chapter: Chapter) => void;
}

function Row({
  chapter,
  wide,
  isNew,
  onOpenDetails,
}: { chapter: Chapter; wide: boolean } & Omit<Props, 'chapters'>) {
  const title = chapter.title ?? `Chapter ${String(chapter.position)}`;
  return (
    <li className="border-b border-line">
      <a
        href={chapter.url}
        aria-label={`${title}, ${STATE_LABELS[chapter.state]}, open details`}
        onClick={(event) => {
          event.preventDefault();
          onOpenDetails(chapter);
        }}
        className="flex min-h-11 items-stretch gap-3 ps-3 text-sm hover:bg-page min-[720px]:ps-5"
      >
        <span aria-hidden="true" className="w-10 shrink-0 self-center font-semibold text-ink-muted">
          {chapter.position}
        </span>
        <span aria-hidden="true" className="flex min-w-0 flex-1 items-center gap-2 py-2">
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
            <span aria-hidden="true" className="w-24 shrink-0 self-center text-ink-muted">
              {chapter.released_at === null ? (
                <span title="Part of the initial import">—</span>
              ) : (
                dateOnly(chapter.released_at)
              )}
            </span>
            <span aria-hidden="true" className="w-28 shrink-0 self-center text-ink-muted">
              {relative(chapter.updated_at)}
            </span>
          </>
        )}
        <span aria-hidden="true" className="flex">
          <StatusTab state={chapter.state} />
        </span>
      </a>
    </li>
  );
}

/** Chapter table in normal mode (spec section 10): rows open chapter details. Never shows errors. */
export function ChapterTable({ chapters, isNew, onOpenDetails }: Props) {
  const wide = useMediaQuery(DESKTOP);
  return (
    <div>
      {wide && (
        <div
          aria-hidden="true"
          className="flex gap-3 border-b border-line bg-page py-2 ps-5 text-xs font-semibold uppercase tracking-wide text-ink-muted"
        >
          <span className="w-10 shrink-0">#</span>
          <span className="flex-1">Title</span>
          <span className="w-24 shrink-0">Added</span>
          <span className="w-28 shrink-0">Updated</span>
          <span className="w-12 shrink-0" />
        </div>
      )}
      <ul>
        {chapters.map((chapter) => (
          <Row
            key={chapter.id}
            chapter={chapter}
            wide={wide}
            isNew={isNew}
            onOpenDetails={onOpenDetails}
          />
        ))}
      </ul>
    </div>
  );
}
