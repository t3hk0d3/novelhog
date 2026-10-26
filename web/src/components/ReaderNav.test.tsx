import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { makeChapters } from '../test/fixtures';
import { ReaderNav } from './ReaderNav';

function show(
  props: Partial<React.ComponentProps<typeof ReaderNav>> & { states?: Record<number, never> } = {},
) {
  const onPrevious = vi.fn();
  const onNext = vi.fn();
  render(
    <ReaderNav
      chapters={makeChapters(5, { 2: 'error', 3: 'pending', 4: 'not_requested' })}
      index={2}
      hidden={false}
      wide={false}
      onPrevious={onPrevious}
      onNext={onNext}
      {...props}
    />,
  );
  return { onPrevious, onNext };
}

const nav = () => screen.getByRole('navigation', { name: 'Chapters' });

describe('ReaderNav', () => {
  describe('the position', () => {
    it('says "3 of 5", without the word chapters, inside the bar', () => {
      show();
      const bar = screen.getByRole('progressbar', { name: 'Reading position' });
      expect(within(bar).getByText('3 of 5')).toBeInTheDocument();
    });

    it('is a progress bar that screen readers can read in full', () => {
      show();
      const bar = screen.getByRole('progressbar', { name: 'Reading position' });
      expect(bar).toHaveAttribute('aria-valuenow', '3');
      expect(bar).toHaveAttribute('aria-valuemax', '5');
      expect(bar).toHaveAttribute('aria-valuetext', 'Chapter 3 of 5');
    });

    it('is as tall as the buttons', () => {
      show();
      expect(screen.getByRole('progressbar')).toHaveClass('min-h-11');
    });

    it('is filled up to the end of the open chapter', () => {
      show({ index: 2 });
      expect(screen.getByTestId('progress-fill').style.width).toBe('60%');
    });

    it('is nearly empty at the first chapter of many, and full at the last', () => {
      show({ chapters: makeChapters(500), index: 0 });
      expect(screen.getByTestId('progress-fill').style.width).toBe('0.2%');
      show({ index: 4 });
      expect(screen.getAllByTestId('progress-fill')[1]?.style.width).toBe('100%');
    });

    it('has a distinct marker at the end of the fill, which stays visible even when nearly empty', () => {
      show({ chapters: makeChapters(500), index: 0 });
      const marker = screen.getByTestId('position-marker');
      expect(marker.style.left).toBe('0.2%');
      expect(marker).toHaveClass('w-1.5');
      // Two short ticks at the top and bottom edges: nothing crosses the text in the middle.
      const ticks = marker.querySelectorAll('.bg-ink');
      expect(ticks).toHaveLength(2);
      for (const tick of ticks) expect(tick).toHaveClass('h-2');
    });

    it('does not colour chapters by their download state', () => {
      show();
      expect(screen.queryAllByTestId('state-segment')).toHaveLength(0);
    });
  });

  describe('Previous and Next', () => {
    it('call their handlers', async () => {
      const { onPrevious, onNext } = show();
      await userEvent.click(screen.getByRole('button', { name: 'Previous chapter' }));
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(onPrevious).toHaveBeenCalledOnce();
      expect(onNext).toHaveBeenCalledOnce();
    });

    it('are 44 px tall at least', () => {
      show();
      expect(screen.getByRole('button', { name: 'Previous chapter' })).toHaveClass('min-h-11');
      expect(screen.getByRole('button', { name: 'Next chapter' })).toHaveClass('min-h-11');
    });

    it('Previous is greyed out and disabled on the first chapter', () => {
      show({ index: 0 });
      expect(screen.getByRole('button', { name: 'Previous chapter' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Previous chapter' })).toHaveClass(
        'disabled:opacity-40',
      );
      expect(screen.getByRole('button', { name: 'Next chapter' })).toBeEnabled();
    });

    it('Next is greyed out and disabled on the last chapter', () => {
      show({ index: 4 });
      expect(screen.getByRole('button', { name: 'Next chapter' })).toBeDisabled();
    });

    it('show only chevrons on a phone, and the words too from desktop widths', () => {
      show({ wide: false });
      expect(screen.getByRole('button', { name: 'Previous chapter' })).not.toHaveTextContent(
        'Previous',
      );
      show({ wide: true });
      expect(screen.getAllByRole('button', { name: 'Previous chapter' })[1]).toHaveTextContent(
        'Previous',
      );
    });
  });

  describe('hiding', () => {
    it('slides off the bottom edge when hidden, but comes back while focus is inside it', () => {
      show({ hidden: true });
      expect(nav()).toHaveClass('translate-y-full', 'focus-within:translate-y-0');
    });

    it('is in place when not hidden', () => {
      show({ hidden: false });
      expect(nav()).not.toHaveClass('translate-y-full');
    });

    it('does not animate for people who asked for less motion', () => {
      show();
      expect(nav()).toHaveClass('motion-reduce:transition-none');
    });
  });
});
