import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';
import { api, type Novel as NovelData } from '../api/client';
import { isDeletedNovel, type DeletedNovel } from '../api/events';
import { ChapterFilters } from '../components/ChapterFilters';
import { NovelHeader } from '../components/NovelHeader';
import { OverviewBar } from '../components/OverviewBar';
import { RangeChips } from '../components/RangeChips';
import { useChapters } from '../hooks/useChapters';
import { useChapterView, type ChapterView } from '../hooks/useChapterView';
import { useLiveReady } from '../hooks/useLiveEvents';
import { errorMessage } from '../lib/errors';
import type { Chapter } from '../api/client';

function ChapterList({ chapters }: { chapters: Chapter[] }) {
  return (
    <ul>
      {chapters.map((chapter) => (
        <li key={chapter.id} className="flex gap-3 border-b border-line py-1.5 text-sm">
          <span className="w-12 font-semibold text-ink-muted">{chapter.position}</span>
          <span>{chapter.title ?? `Chapter ${String(chapter.position)}`}</span>
        </li>
      ))}
    </ul>
  );
}

/** Chapters of the chosen range, or every match under range headings. Task 35 swaps in the table. */
function Results({ view }: { view: ChapterView }) {
  return (
    <section aria-label="Chapters">
      {view.filterActive ? (
        view.groups.length === 0 ? (
          <p className="text-ink-muted">No chapters match.</p>
        ) : (
          view.groups.map((group) => (
            <div key={group.range.index}>
              <h3 className="mt-3 font-semibold">{`${String(group.range.from)}–${String(group.range.to)}`}</h3>
              <ChapterList chapters={group.chapters} />
            </div>
          ))
        )
      ) : (
        <ChapterList chapters={view.rangeChapters} />
      )}
    </section>
  );
}

function emptyMessage(novel: NovelData): string {
  if (novel.importing) return 'Importing…';
  if (novel.import_error !== null) return `Import failed: ${novel.import_error}`;
  return 'No chapters yet';
}

function NovelPage({ novel }: { novel: NovelData }) {
  const chapters = useChapters(novel.id);
  // What was new before this visit marked the novel seen; frozen for the visit.
  const [newBeforeVisit] = useState(novel.new_pages);
  const view = useChapterView(chapters.data ?? [], newBeforeVisit);
  const seenSent = useRef(false);

  useEffect(() => {
    if (seenSent.current) return;
    seenSent.current = true;
    void api.patchNovel(novel.id, { seen: true }).catch(() => {
      // Marking seen is best effort; the next visit tries again.
    });
  }, [novel.id]);

  const all = chapters.data ?? [];
  return (
    <div className="flex flex-col gap-4">
      <NovelHeader novel={novel} />
      {chapters.isError && (
        <p role="alert" className="text-danger">
          {errorMessage(chapters.error)}
        </p>
      )}
      <OverviewBar
        chapters={all}
        ranges={view.ranges}
        range={view.range}
        matches={view.filterActive ? view.matches : null}
        onSelectRange={view.setRange}
      />
      {chapters.data?.length === 0 ? (
        <p className="rounded-xl border border-dashed border-line p-8 text-center text-ink-muted">
          {emptyMessage(novel)}
        </p>
      ) : (
        <>
          <ChapterFilters
            query={view.query}
            onQuery={view.setQuery}
            states={view.states}
            onToggleState={view.toggleState}
            newOnly={view.newOnly}
            onToggleNew={view.toggleNew}
            newCount={view.newCount}
            counts={view.stateCounts}
          />
          <RangeChips
            ranges={view.ranges}
            range={view.range}
            hidden={view.filterActive}
            onSelectRange={view.setRange}
          />
          <Results view={view} />
        </>
      )}
    </div>
  );
}

function Deleted() {
  return (
    <div className="rounded-xl border border-dashed border-line p-8 text-center">
      <p className="mb-4 font-semibold">This novel was deleted</p>
      <Link to="/" className="text-accent underline">
        Back to the library
      </Link>
    </div>
  );
}

/** The novel page (`/novels/:id`, spec section 10, screen 3). */
export function Novel() {
  const { id = '' } = useParams();
  const novel = useQuery<NovelData | DeletedNovel>({
    queryKey: ['novel', id],
    queryFn: () => api.getNovel(id),
    enabled: useLiveReady(),
  });

  // Check for the deleted marker before NovelPage mounts the chapter query: a removed active
  // query would refetch and get a 404.
  if (isDeletedNovel(novel.data)) return <Deleted />;
  if (novel.isError && novel.data === undefined) {
    return (
      <p role="alert" className="text-danger">
        {errorMessage(novel.error)}
      </p>
    );
  }
  if (novel.data === undefined) return <p className="text-ink-muted">Loading…</p>;
  return <NovelPage key={novel.data.id} novel={novel.data} />;
}
