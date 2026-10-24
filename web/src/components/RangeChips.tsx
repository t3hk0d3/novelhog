import { useMediaQuery } from '../hooks/useMediaQuery';
import type { ChapterRange } from '../hooks/useChapterView';

const WIDE = '(min-width: 720px)';

function pct(count: number, total: number): string {
  return `${String((count / total) * 100)}%`;
}

function MiniBar({ range }: { range: ChapterRange }) {
  const { counts } = range;
  const total = range.to - range.from + 1;
  return (
    <span
      data-mini-bar
      aria-hidden="true"
      className="flex h-1 w-full overflow-hidden rounded-full bg-[var(--status-not-requested-tab)]"
    >
      <span style={{ width: pct(counts.downloaded, total) }} className="bg-accent" />
      <span
        style={{ width: pct(counts.pending + counts.downloading, total) }}
        className="bg-accent/40"
      />
      <span style={{ width: pct(counts.error, total) }} className="bg-[var(--status-error-tab)]" />
    </span>
  );
}

interface RangeChipsProps {
  ranges: ChapterRange[];
  range: number | null;
  /** True while a search or filter is active. */
  hidden: boolean;
  onSelectRange: (index: number) => void;
}

/** One chip per 50 positions: wraps on desktop, scrolls sideways on phones (spec section 10). */
export function RangeChips({ ranges, range, hidden, onSelectRange }: RangeChipsProps) {
  const wide = useMediaQuery(WIDE);
  if (hidden || ranges.length === 0) return null;
  return (
    <div
      role="group"
      aria-label="Chapter ranges"
      className={`flex gap-2 ${wide ? 'flex-wrap' : 'flex-nowrap overflow-x-auto pb-1'}`}
    >
      {ranges.map((r) => (
        <button
          key={r.index}
          type="button"
          aria-pressed={r.index === range}
          onClick={() => {
            onSelectRange(r.index);
          }}
          className={`flex shrink-0 flex-col gap-1.5 rounded-lg border px-3 py-1.5 text-sm font-semibold ${
            r.index === range ? 'border-ink bg-ink text-page' : 'border-line bg-surface text-ink'
          }`}
        >
          {`${String(r.from)}–${String(r.to)}`}
          <MiniBar range={r} />
        </button>
      ))}
    </div>
  );
}
