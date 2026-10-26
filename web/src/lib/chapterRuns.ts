import type { Chapter, ChapterState } from '../api/client';

/** The colour of a chapter state in the strips that show a whole novel at a glance. */
export const SEGMENT_COLOR: Record<ChapterState, string> = {
  downloaded: 'var(--accent)',
  pending: 'color-mix(in srgb, var(--accent) 40%, transparent)',
  downloading: 'var(--status-downloading-tab)',
  error: 'var(--status-error-tab)',
  not_requested: 'var(--status-not-requested-tab)',
};

export interface Run {
  state: ChapterState;
  matched: boolean;
  /** Index of the run's first chapter in the list, and how many chapters it holds. */
  start: number;
  length: number;
}

/** Runs of consecutive chapters with the same state (and, with `split`, the same match). */
export function runsOf(
  chapters: Chapter[],
  matched: (chapter: Chapter) => boolean,
  split: boolean,
) {
  const runs: Run[] = [];
  chapters.forEach((chapter, index) => {
    const isMatch = split ? matched(chapter) : true;
    const last = runs[runs.length - 1];
    if (last?.state === chapter.state && last.matched === isMatch) last.length += 1;
    else runs.push({ state: chapter.state, matched: isMatch, start: index, length: 1 });
  });
  return runs;
}
