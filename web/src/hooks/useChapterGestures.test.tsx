import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EXIT_MS, useChapterGestures } from './useChapterGestures';

/** A page with a gesture surface that can be removed and put back (as the reader does). */
function Harness({ show }: { show: boolean }) {
  const { ref, exiting, entering } = useChapterGestures({
    hasPrevious: true,
    hasNext: true,
    reducedMotion: false,
    onCommit: () => undefined,
  });
  return (
    <>
      {show && <div ref={ref} data-testid="surface" />}
      <output data-testid="flags">{`${String(exiting)}|${String(entering)}`}</output>
    </>
  );
}

const flags = () => screen.getByTestId('flags').textContent;

/** A long, deliberate swipe to the left on the surface: it commits to the next chapter. */
function swipeLeft() {
  const surface = screen.getByTestId('surface');
  const at = (x: number) => ({ touches: [{ clientX: x, clientY: 300 }] });
  fireEvent.touchStart(surface, at(500));
  fireEvent.touchMove(surface, at(400));
  fireEvent.touchMove(surface, at(200));
  fireEvent.touchEnd(surface, { touches: [] });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 400 });
});

afterEach(() => {
  vi.useRealTimers();
  Reflect.deleteProperty(HTMLElement.prototype, 'clientWidth');
});

describe('useChapterGestures', () => {
  describe('a committed swipe', () => {
    it('slides the page out, then flags the next one as entering, then settles', () => {
      render(<Harness show />);
      swipeLeft();
      expect(flags()).toBe('next|null');
      act(() => {
        vi.advanceTimersByTime(EXIT_MS);
      });
      expect(flags()).toBe('null|next');
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(flags()).toBe('null|null');
    });
  });

  describe('when the surface goes away while the page is sliding out', () => {
    it('does not leave the page flagged as exiting (it would come back invisible)', () => {
      const { rerender } = render(<Harness show />);
      swipeLeft();
      expect(flags()).toBe('next|null');
      rerender(<Harness show={false} />);
      expect(flags()).toBe('null|null');
      rerender(<Harness show />);
      expect(flags()).toBe('null|null');
    });
  });

  describe('when the surface goes away while the next page slides in', () => {
    it('does not leave the page flagged as entering (it would keep a stale animation)', () => {
      const { rerender } = render(<Harness show />);
      swipeLeft();
      act(() => {
        vi.advanceTimersByTime(EXIT_MS);
      });
      expect(flags()).toBe('null|next');
      rerender(<Harness show={false} />);
      expect(flags()).toBe('null|null');
    });
  });
});
