import type { ChapterState } from '../api/client';
import { CHAPTER_STATES, type StateCounts } from '../hooks/useChapterView';
import { StatusIcon } from './StatusIcon';

interface ChapterFiltersProps {
  query: string;
  onQuery: (query: string) => void;
  states: ReadonlySet<ChapterState>;
  onToggleState: (state: ChapterState) => void;
  newOnly: boolean;
  onToggleNew: () => void;
  /** Chapters released since the last visit. */
  newCount: number;
  counts: StateCounts;
}

const chip = (pressed: boolean) =>
  `inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-semibold ${
    pressed ? 'border-accent bg-accent/15 text-ink' : 'border-line bg-surface text-ink'
  }`;

/** Search field plus a filter chip per state (icon and count) and "New" (spec section 10). */
export function ChapterFilters(props: ChapterFiltersProps) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        type="search"
        aria-label="Search number or title"
        placeholder="Search number or title"
        value={props.query}
        onChange={(event) => {
          props.onQuery(event.target.value);
        }}
        className="min-w-0 flex-1 basis-48 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm"
      />
      {CHAPTER_STATES.map((state) => (
        <button
          key={state}
          type="button"
          aria-pressed={props.states.has(state)}
          onClick={() => {
            props.onToggleState(state);
          }}
          className={chip(props.states.has(state))}
        >
          <StatusIcon state={state} /> <span>{props.counts[state]}</span>
        </button>
      ))}
      <button
        type="button"
        aria-pressed={props.newOnly}
        onClick={props.onToggleNew}
        className={chip(props.newOnly)}
      >
        New <span>{props.newCount}</span>
      </button>
    </div>
  );
}
