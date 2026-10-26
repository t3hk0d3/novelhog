import type { MouseEvent } from 'react';
import type { Chapter } from '../api/client';
import { rangeIndexOf, type ChapterRange } from '../hooks/useChapterView';
import { SEGMENT_COLOR, runsOf } from '../lib/chapterRuns';
import { STATE_LABELS } from './StatusIcon';

const MAX_SPOKEN_RUNS = 12;

function summary(chapters: Chapter[]): string {
  if (chapters.length === 0) return 'No chapters';
  const runs = runsOf(chapters, () => true, false);
  const spoken = runs.slice(0, MAX_SPOKEN_RUNS).map((run) => {
    const from = chapters[run.start]?.position ?? 0;
    const to = chapters[run.start + run.length - 1]?.position ?? from;
    return `${from === to ? String(from) : `${String(from)}–${String(to)}`} ${run.state === 'not_requested' ? 'not requested' : STATE_LABELS[run.state].toLowerCase()}`;
  });
  const more = runs.length - spoken.length;
  return `Chapters ${spoken.join(', ')}${more > 0 ? `, and ${String(more)} more runs` : ''}`;
}

function tickPositions(chapters: Chapter[]): number[] {
  const n = chapters.length;
  if (n === 0) return [];
  const indexes = [0, Math.floor(n / 3), Math.floor((2 * n) / 3), n - 1];
  const positions = indexes.map((i) => chapters[i]?.position ?? 0);
  return [...new Set(positions)];
}

interface OverviewBarProps {
  chapters: Chapter[];
  ranges: ChapterRange[];
  /** Index of the range shown below (the frame), or null. */
  range: number | null;
  /** The chapters matching an active search or filter, or null when none is active. */
  matches: Chapter[] | null;
  onSelectRange: (index: number) => void;
}

/** Whole-novel strip: one segment per run of equal states, a frame on the shown range. */
export function OverviewBar({ chapters, ranges, range, matches, onSelectRange }: OverviewBarProps) {
  const n = chapters.length;
  const matchedIds = matches && new Set(matches.map((c) => c.id));
  const segments = runsOf(chapters, (c) => matchedIds?.has(c.id) ?? true, matchedIds !== null);
  const shown = ranges.find((r) => r.index === range);
  const frameStart = shown ? chapters.findIndex((c) => c.position >= shown.from) : -1;
  const frameLength = shown
    ? chapters.filter((c) => c.position >= shown.from && c.position <= shown.to).length
    : 0;

  function jump(event: MouseEvent<HTMLDivElement>) {
    const box = event.currentTarget.getBoundingClientRect();
    if (n === 0 || box.width <= 0) return;
    const fraction = Math.min(Math.max((event.clientX - box.left) / box.width, 0), 0.999999);
    const chapter = chapters[Math.floor(fraction * n)];
    if (chapter) onSelectRange(rangeIndexOf(chapter.position));
  }

  return (
    <div>
      <div className="relative py-1.5">
        <div
          role="img"
          aria-label={summary(chapters)}
          onClick={jump}
          className="flex h-3 cursor-pointer overflow-hidden rounded-full bg-[var(--status-not-requested-tab)]"
        >
          {segments.map((run) => (
            <div
              key={run.start}
              data-segment={run.state}
              data-dimmed={String(!run.matched)}
              style={{
                flex: `${String(run.length)} 1 0`,
                minWidth: '2px',
                background: SEGMENT_COLOR[run.state],
                opacity: run.matched ? 1 : 0.25,
              }}
            />
          ))}
        </div>
        {shown && frameStart >= 0 && (
          <div
            data-testid="overview-frame"
            style={{
              left: `${String((frameStart / n) * 100)}%`,
              width: `${String((frameLength / n) * 100)}%`,
            }}
            className="pointer-events-none absolute inset-y-0 min-w-1 rounded border-2 border-ink"
          />
        )}
      </div>
      <div aria-hidden="true" className="flex justify-between text-xs text-ink-muted">
        {tickPositions(chapters).map((position) => (
          <span key={position} data-tick>
            {position}
          </span>
        ))}
      </div>
    </div>
  );
}
