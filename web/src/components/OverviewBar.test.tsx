import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { buildRanges } from '../hooks/useChapterView';
import { makeChapters } from '../test/fixtures';
import { OverviewBar } from './OverviewBar';

interface BarOptions {
  range?: number | null;
  matches?: Chapter[] | null;
  onSelectRange?: (index: number) => void;
}

function bar(
  chapters: Chapter[],
  { range = null, matches = null, onSelectRange = vi.fn() }: BarOptions = {},
) {
  return render(
    <OverviewBar
      chapters={chapters}
      ranges={buildRanges(chapters)}
      range={range}
      matches={matches}
      onSelectRange={onSelectRange}
    />,
  );
}

const segments = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLElement>('[data-segment]'),
];

describe('OverviewBar', () => {
  it('draws one segment per run of equal states with proportional widths (min 2 px)', () => {
    const { container } = bar(makeChapters(100, { 41: 'error', 42: 'error', 43: 'error' }));
    const found = segments(container);
    expect(found.map((s) => [s.dataset.segment, s.style.flexGrow])).toEqual([
      ['downloaded', '40'],
      ['error', '3'],
      ['downloaded', '57'],
    ]);
    expect(found.every((s) => s.style.minWidth === '2px')).toBe(true);
  });

  it('places the frame on the current range', () => {
    bar(makeChapters(100), { range: 1 });
    const frame = screen.getByTestId('overview-frame');
    expect(frame.style.left).toBe('50%');
    expect(frame.style.width).toBe('50%');
  });

  it('jumps to the range containing the clicked chapter', () => {
    const onSelectRange = vi.fn();
    bar(makeChapters(100), { range: 1, onSelectRange });
    const strip = screen.getByRole('img');
    vi.spyOn(strip, 'getBoundingClientRect').mockReturnValue({
      left: 100,
      width: 200,
    } as DOMRect);
    fireEvent.click(strip, { clientX: 110 });
    expect(onSelectRange).toHaveBeenLastCalledWith(0);
    fireEvent.click(strip, { clientX: 290 });
    expect(onSelectRange).toHaveBeenLastCalledWith(1);
  });

  it('dims segments that do not match an active search or filter', () => {
    const chapters = makeChapters(10, { 5: 'error' });
    const { container } = bar(chapters, { matches: [chapters[4] as Chapter] });
    expect(segments(container).map((s) => [s.dataset.segment, s.dataset.dimmed])).toEqual([
      ['downloaded', 'true'],
      ['error', 'false'],
      ['downloaded', 'true'],
    ]);
  });

  it('does not dim anything without an active search or filter', () => {
    const { container } = bar(makeChapters(10, { 5: 'error' }));
    expect(segments(container).every((s) => s.dataset.dimmed === 'false')).toBe(true);
  });

  it('updates when the chapters change', () => {
    const chapters = makeChapters(10);
    const { container, rerender } = bar(chapters);
    expect(segments(container)).toHaveLength(1);
    rerender(
      <OverviewBar
        chapters={makeChapters(10, { 10: 'pending' })}
        ranges={[]}
        range={null}
        matches={null}
        onSelectRange={vi.fn()}
      />,
    );
    expect(segments(container)).toHaveLength(2);
  });

  it('exposes a text alternative summarising the runs', () => {
    bar(makeChapters(100, { 41: 'error', 42: 'error', 43: 'error' }));
    expect(screen.getByRole('img')).toHaveAccessibleName(
      'Chapters 1–40 downloaded, 41–43 error, 44–100 downloaded',
    );
  });

  it('labels the start, the end and two evenly spaced points', () => {
    const { container } = bar(makeChapters(90));
    const ticks = [...container.querySelectorAll('[data-tick]')].map((t) => t.textContent);
    expect(ticks).toEqual(['1', '31', '61', '90']);
  });

  describe('With zero chapters (RF-5)', () => {
    it('renders an empty bar without errors and no frame', () => {
      const { container } = bar([]);
      expect(screen.getByRole('img')).toHaveAccessibleName('No chapters');
      expect(segments(container)).toHaveLength(0);
      expect(screen.queryByTestId('overview-frame')).toBeNull();
    });
  });
});
