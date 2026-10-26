import { describe, expect, it } from 'vitest';
import { HIDE_PX, JUMP_PX, SHOW_PX, startBar, stepBar, type BarState } from './barVisibility';

const MAX = 5000;

/** Scrolls through `ys` (page positions) in order and returns the final state. */
function scroll(ys: number[], from: BarState = startBar(100)): BarState {
  return ys.reduce((state, y) => stepBar(state, y, MAX), from);
}

describe('stepBar', () => {
  describe('at the top of the page', () => {
    it('starts visible', () => {
      expect(startBar(0).hidden).toBe(false);
    });

    it('stays visible while scrolling within the top zone', () => {
      expect(scroll([2, 4, 6], startBar(0)).hidden).toBe(false);
    });
  });

  describe('scrolling down', () => {
    it('hides after a deliberate distance, not before', () => {
      expect(scroll([100 + HIDE_PX - 5]).hidden).toBe(false);
      expect(scroll([100 + HIDE_PX + 5]).hidden).toBe(true);
    });

    it('adds up many small steps', () => {
      const ys = Array.from({ length: 12 }, (_, i) => 100 + (i + 1) * 4); // 4 px at a time
      expect(scroll(ys).hidden).toBe(true);
    });
  });

  describe('scrolling up', () => {
    const hidden = scroll([150, 200, 250]);

    it('starts hidden for the test', () => {
      expect(hidden.hidden).toBe(true);
    });

    it('shows again after a short distance, not before', () => {
      expect(stepBar(hidden, 250 - SHOW_PX + 4, MAX).hidden).toBe(true);
      expect(stepBar(hidden, 250 - SHOW_PX - 2, MAX).hidden).toBe(false);
    });

    it('does not count a downward wobble as the start of a new direction too early', () => {
      const wobbled = scroll([246, 249, 245, 248], hidden);
      expect(wobbled.hidden).toBe(true);
    });
  });

  describe('a change of direction', () => {
    it('starts counting from the turn: scrolling down then a little up does not hide', () => {
      const state = scroll([110, 120, 112]); // 20 down, then 8 up
      expect(state.hidden).toBe(false);
    });
  });

  describe('at the very end of the page', () => {
    it('is visible even after scrolling down', () => {
      expect(scroll([150, 200, MAX]).hidden).toBe(false);
    });

    it('shows when the end is reached from a hidden state', () => {
      const hidden = scroll([150, 200, 250]);
      expect(stepBar(hidden, MAX - 4, MAX).hidden).toBe(false);
    });
  });

  describe('overscroll bounce beyond the ends', () => {
    it('is clamped: a bounce past the top does not hide or flicker the bar', () => {
      expect(scroll([-30, -10, -40]).hidden).toBe(false);
    });

    it('is clamped past the bottom: positions beyond the end count as the end', () => {
      const state = scroll([MAX + 40, MAX + 10, MAX + 60]);
      expect(state.hidden).toBe(false);
    });
  });

  describe('a jump (the page set the position, not a finger)', () => {
    it('is ignored: the bar neither hides nor shows', () => {
      const hidden = scroll([150, 200, 250]);
      expect(stepBar(hidden, 250 + JUMP_PX + 50, MAX).hidden).toBe(true);
      const visible = scroll([105]);
      expect(stepBar(visible, 105 + JUMP_PX + 50, MAX).hidden).toBe(false);
    });

    it('re-anchors, so the next small scroll is measured from where the page landed', () => {
      const landed = stepBar(scroll([105]), 3000, MAX);
      expect(stepBar(landed, 3000 + HIDE_PX + 5, MAX).hidden).toBe(true);
    });
  });

  describe('a page that does not scroll', () => {
    it('stays visible', () => {
      expect(stepBar(startBar(0), 0, 0).hidden).toBe(false);
      expect(stepBar(startBar(0), 50, 0).hidden).toBe(false);
    });
  });
});
