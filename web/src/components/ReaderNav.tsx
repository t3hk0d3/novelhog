import type { Chapter } from '../api/client';
import { ChevronLeftIcon, ChevronRightIcon } from './icons';

interface Props {
  /** The novel's whole chapter list, in position order. */
  chapters: Chapter[];
  /** Index of the open chapter in `chapters`. */
  index: number;
  /** Slid off the bottom edge (a phone while reading down the page); focus inside brings it back. */
  hidden: boolean;
  /** Desktop widths: the buttons say Previous and Next as well as showing chevrons. */
  wide: boolean;
  onPrevious: () => void;
  onNext: () => void;
}

const button =
  'flex min-h-11 items-center justify-center gap-1 rounded-lg border border-line text-sm font-semibold disabled:opacity-40';

/**
 * The reader's bottom bar: Previous, where you are in the novel, Next. The middle is a bar as tall
 * as the buttons, filled up to the open chapter and saying "3 of 500" inside it.
 */
export function ReaderNav({ chapters, index, hidden, wide, onPrevious, onNext }: Props) {
  const n = chapters.length;
  // Filled to the end of the open chapter (a tenth of a percent is plenty of precision).
  const percent = String(Math.round(((index + 1) * 1000) / Math.max(n, 1)) / 10);
  return (
    <nav
      aria-label="Chapters"
      className={`sticky bottom-0 flex items-stretch gap-2 border-t border-line bg-page px-4 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] transition-transform duration-200 motion-reduce:transition-none ${
        hidden ? 'translate-y-full focus-within:translate-y-0' : ''
      }`}
    >
      <button
        type="button"
        aria-label="Previous chapter"
        disabled={index <= 0}
        onClick={onPrevious}
        className={`${button} ${wide ? 'px-4' : 'w-16'}`}
      >
        <ChevronLeftIcon />
        {wide && 'Previous'}
      </button>
      <div
        role="progressbar"
        aria-label="Reading position"
        aria-valuemin={1}
        aria-valuemax={n}
        aria-valuenow={index + 1}
        aria-valuetext={`Chapter ${String(index + 1)} of ${String(n)}`}
        className="relative flex min-h-11 min-w-0 flex-1 items-center justify-center overflow-hidden rounded-lg border border-line bg-surface"
      >
        <div
          data-testid="progress-fill"
          style={{ width: `${percent}%` }}
          className="absolute inset-y-0 left-0 bg-[color-mix(in_srgb,var(--accent)_28%,transparent)]"
        />
        <span aria-hidden="true" className="relative text-sm font-semibold">
          {`${String(index + 1)} of ${String(n)}`}
        </span>
      </div>
      <button
        type="button"
        aria-label="Next chapter"
        disabled={index >= n - 1}
        onClick={onNext}
        className={`${button} ${wide ? 'px-4' : 'w-16'}`}
      >
        {wide && 'Next'}
        <ChevronRightIcon />
      </button>
    </nav>
  );
}
