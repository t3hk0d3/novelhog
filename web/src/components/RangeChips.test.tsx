import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildRanges } from '../hooks/useChapterView';
import { makeChapters } from '../test/fixtures';
import { RangeChips } from './RangeChips';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubWide(wide: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: wide, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
}

function chips(count: number, props: { range?: number | null; hidden?: boolean } = {}) {
  const onSelectRange = vi.fn();
  render(
    <RangeChips
      ranges={buildRanges(makeChapters(count, { 3: 'error' }))}
      range={props.range ?? null}
      hidden={props.hidden ?? false}
      onSelectRange={onSelectRange}
    />,
  );
  return onSelectRange;
}

describe('RangeChips', () => {
  it('renders one chip per 50 positions, the last ending at the highest position', () => {
    stubWide(true);
    chips(130);
    expect(screen.getAllByRole('button').map((b) => b.textContent)).toEqual([
      '1–50',
      '51–100',
      '101–130',
    ]);
  });

  it('marks the current range pressed and selects a range on click', async () => {
    stubWide(true);
    const onSelectRange = chips(130, { range: 2 });
    expect(screen.getByRole('button', { name: '101–130' })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: '1–50' }));
    expect(onSelectRange).toHaveBeenCalledWith(0);
  });

  it('draws a mini bar of downloaded, pending and error chapters', () => {
    stubWide(true);
    chips(100);
    const bar = screen.getByRole('button', { name: '1–50' }).querySelector('[data-mini-bar]');
    expect(bar?.children).toHaveLength(3);
    expect((bar?.children[2] as HTMLElement).style.width).toBe('2%');
  });

  it('wraps onto several rows at 720 px and wider', () => {
    stubWide(true);
    chips(130);
    expect(screen.getByRole('group', { name: 'Chapter ranges' })).toHaveClass('flex-wrap');
  });

  it('scrolls sideways below 720 px', () => {
    stubWide(false);
    chips(130);
    const list = screen.getByRole('group', { name: 'Chapter ranges' });
    expect(list).toHaveClass('overflow-x-auto');
    expect(list).not.toHaveClass('flex-wrap');
  });

  it('is hidden while a search or filter is active', () => {
    stubWide(true);
    chips(130, { hidden: true });
    expect(screen.queryByRole('group', { name: 'Chapter ranges' })).toBeNull();
  });

  describe('With zero chapters (RF-5)', () => {
    it('renders no chips', () => {
      stubWide(true);
      chips(0);
      expect(screen.queryByRole('button')).toBeNull();
    });
  });
});
