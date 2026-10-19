import { useMemo, useState } from 'react';
import type { Chapter, ChapterState } from '../api/client';

export const RANGE_SIZE = 50;

export const CHAPTER_STATES: readonly ChapterState[] = [
  'not_requested',
  'pending',
  'downloading',
  'downloaded',
  'error',
];

export type StateCounts = Record<ChapterState, number>;

export interface ChapterRange {
  index: number;
  /** First and last position of the range (the last range ends at the highest position). */
  from: number;
  to: number;
  counts: StateCounts;
}

export interface ChapterGroup {
  range: ChapterRange;
  chapters: Chapter[];
}

const emptyCounts = (): StateCounts => ({
  not_requested: 0,
  pending: 0,
  downloading: 0,
  downloaded: 0,
  error: 0,
});

export const rangeIndexOf = (position: number): number => Math.floor((position - 1) / RANGE_SIZE);

export function buildRanges(chapters: Chapter[]): ChapterRange[] {
  const ranges = new Map<number, ChapterRange>();
  for (const chapter of chapters) {
    const index = rangeIndexOf(chapter.position);
    let range = ranges.get(index);
    if (!range) {
      range = { index, from: index * RANGE_SIZE + 1, to: chapter.position, counts: emptyCounts() };
      ranges.set(index, range);
    }
    range.to = Math.max(range.to, chapter.position);
    range.counts[chapter.state] += 1;
  }
  return [...ranges.values()].sort((a, b) => a.index - b.index);
}

/** The oldest release time still counted as new: that of the `newCount`-th newest release. */
function newThreshold(chapters: Chapter[], newCount: number): string | null {
  if (newCount <= 0) return null;
  const released = chapters
    .flatMap((c) => (c.released_at === null ? [] : [c.released_at]))
    .sort()
    .reverse();
  return released[Math.min(newCount, released.length) - 1] ?? null;
}

function matchesQuery(chapter: Chapter, query: string): boolean {
  if (query === '') return true;
  const number = /^#?(\d+)$/.exec(query);
  if (number && chapter.position === Number(number[1])) return true;
  return (chapter.title ?? '').toLowerCase().includes(query.toLowerCase());
}

/**
 * Range, search and filter selection over a novel's cached chapter list (position order).
 * `newCount` is how many chapters were released since the last visit, as the novel reported
 * before the page marked it seen.
 */
export function useChapterView(chapters: Chapter[], newCount: number) {
  const [query, setQuery] = useState('');
  const [states, setStates] = useState<ReadonlySet<ChapterState>>(new Set());
  const [newOnly, setNewOnly] = useState(false);
  const [chosenRange, setRange] = useState<number | null>(null);

  const ranges = useMemo(() => buildRanges(chapters), [chapters]);
  const stateCounts = useMemo(() => {
    const counts = emptyCounts();
    for (const chapter of chapters) counts[chapter.state] += 1;
    return counts;
  }, [chapters]);
  const threshold = useMemo(() => newThreshold(chapters, newCount), [chapters, newCount]);
  const newChapters = useMemo(
    () =>
      new Set(
        threshold === null
          ? []
          : chapters.filter((c) => c.released_at !== null && c.released_at >= threshold),
      ),
    [chapters, threshold],
  );

  const trimmed = query.trim();
  const filterActive = trimmed !== '' || states.size > 0 || newOnly;
  const matches = useMemo(
    () =>
      filterActive
        ? chapters.filter(
            (c) =>
              matchesQuery(c, trimmed) &&
              (states.size === 0 || states.has(c.state)) &&
              (!newOnly || newChapters.has(c)),
          )
        : chapters,
    [chapters, filterActive, trimmed, states, newOnly, newChapters],
  );

  const lastRange = ranges[ranges.length - 1];
  const range =
    chosenRange !== null && ranges.some((r) => r.index === chosenRange)
      ? chosenRange
      : (lastRange?.index ?? null);
  const rangeChapters = useMemo(
    () => (range === null ? [] : chapters.filter((c) => rangeIndexOf(c.position) === range)),
    [chapters, range],
  );
  const groups = useMemo<ChapterGroup[]>(() => {
    const out: ChapterGroup[] = [];
    for (const chapter of matches) {
      const index = rangeIndexOf(chapter.position);
      const last = out[out.length - 1];
      if (last?.range.index === index) last.chapters.push(chapter);
      else {
        const found = ranges.find((r) => r.index === index);
        if (found) out.push({ range: found, chapters: [chapter] });
      }
    }
    return out;
  }, [matches, ranges]);

  return {
    query,
    setQuery,
    states,
    toggleState: (state: ChapterState) => {
      setStates((current) => {
        const next = new Set(current);
        if (!next.delete(state)) next.add(state);
        return next;
      });
    },
    newOnly,
    toggleNew: () => {
      setNewOnly((current) => !current);
    },
    newCount: newChapters.size,
    stateCounts,
    filterActive,
    matches,
    ranges,
    range,
    setRange,
    rangeChapters,
    groups,
  };
}

export type ChapterView = ReturnType<typeof useChapterView>;
