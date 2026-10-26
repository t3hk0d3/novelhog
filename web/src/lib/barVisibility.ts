/**
 * When the reader's bottom bar slides away: down while the reader scrolls down, back after a
 * little scrolling up, always there at the very top and the very end of the page. Pure, so the
 * thresholds can be tested without a browser.
 */

/** Continuous scrolling down that hides the bar (px). */
export const HIDE_PX = 24;
/** Continuous scrolling up that brings it back (px). */
export const SHOW_PX = 12;
/** A single step bigger than this was made by the page, not by a finger (px). */
export const JUMP_PX = 160;
/** Within this of the top or the end the bar is always shown (px). */
const EDGE_PX = 8;

export interface BarState {
  hidden: boolean;
  /** The position the last step ended at (px, clamped to the page). */
  at: number;
  /** Scrolling accumulated in the current direction: positive down, negative up (px). */
  travel: number;
}

export function startBar(at: number): BarState {
  return { hidden: false, at, travel: 0 };
}

/** The state after the page scrolled to `y`; `max` is the furthest it can scroll. */
export function stepBar(state: BarState, y: number, max: number): BarState {
  if (max <= 0) return { hidden: false, at: 0, travel: 0 };
  // Overscroll bounce (iOS) reports positions beyond the ends and reverses direction there.
  const at = Math.min(Math.max(y, 0), max);
  const delta = at - state.at;
  if (at <= EDGE_PX || at >= max - EDGE_PX) return { hidden: false, at, travel: 0 };
  if (Math.abs(delta) > JUMP_PX) return { ...state, at, travel: 0 };
  // Only a continuous run in one direction counts; a turn starts a new run.
  const travel = delta * state.travel < 0 ? delta : state.travel + delta;
  if (travel >= HIDE_PX) return { hidden: true, at, travel };
  if (travel <= -SHOW_PX) return { hidden: false, at, travel };
  return { hidden: state.hidden, at, travel };
}
