/**
 * The decision logic of the reader's touch gestures, free of the DOM: a horizontal swipe that
 * follows the finger and snaps to the next or previous chapter, and a vertical pull past the end
 * or the start of the text. The hook feeds it pointer positions; it says what to draw and
 * whether a release commits.
 */

export interface Point {
  x: number;
  y: number;
  /** Milliseconds, from any clock. */
  t: number;
}

export interface Bounds {
  /** Width of the reading column. */
  width: number;
  /** Width of the screen; a start near its left or right edge belongs to the system back gesture. */
  viewportWidth: number;
  hasPrevious: boolean;
  hasNext: boolean;
}

/** Whether the page is scrolled to its very top / very bottom right now. */
export interface Edges {
  atTop: boolean;
  atBottom: boolean;
}

export type Commit = 'next' | 'previous';

type Phase = 'undecided' | 'native' | 'horizontal' | 'pull' | 'cancelled';

export interface Gesture {
  bounds: Bounds;
  origin: Point;
  /** The samples of the last RECENT_MS, oldest first; the flick speed comes from them. */
  recent: Point[];
  phase: Phase;
  /** Set once phase is 'pull': which chapter the pull is for. */
  pullDirection: Commit | null;
}

export interface GestureView {
  /** Horizontal offset of the column, px (damped at the ends of the book). */
  offsetX: number;
  pull: {
    direction: Commit;
    distance: number;
    ready: boolean;
    edge: 'first' | 'last' | null;
  } | null;
  /** Set while a horizontal drag pushes against the first or last chapter. */
  edge: 'first' | 'last' | null;
  /** A horizontal drag long enough that releasing now turns the chapter. */
  ready: boolean;
  /** How far a horizontal drag is towards `ready`, 0..1 (also at the ends of the book). */
  progress: number;
}

/** Movement before the gesture picks an axis (px). */
export const LOCK_PX = 10;
/** A start this close to the left or right screen edge is ignored (px). */
export const EDGE_ZONE_PX = 24;
// A chapter change has to be meant: a long drag, or a hard and reasonably long flick.
const COMMIT_FRACTION = 0.6;
const FLICK_SPEED = 0.9; // px per ms
const FLICK_MIN_PX = 100;
// The column resists: it moves ever less per px of finger, and never past SHIFT_CAP of the width
// (about 28% of it where a release commits). SHIFT_SCALE sets how quickly the resistance grows.
const SHIFT_CAP = 0.4;
const SHIFT_SCALE = 0.5;
/** A drag is a swipe only when it is this much more sideways than vertical. */
const HORIZONTAL_BIAS = 1.7;
/** A movement still ambiguous (diagonal) after this far is left to native scrolling (px). */
const GIVE_UP_PX = 40;
const RECENT_MS = 100;
const RUBBER = 0.35;
const RUBBER_MAX_PX = 60;
/** Finger travel that makes a pull ready to commit (px). */
export const PULL_THRESHOLD_PX = 96;
const PULL_RESISTANCE = 0.5;
const PULL_MAX_PX = 140;

const NOT_ACTIVE: GestureView = { offsetX: 0, pull: null, edge: null, ready: false, progress: 0 };

/** Begins a gesture, or null when the touch starts in the system gesture zone. */
export function startGesture(bounds: Bounds, point: Point): Gesture | null {
  if (point.x < EDGE_ZONE_PX || point.x > bounds.viewportWidth - EDGE_ZONE_PX) return null;
  return { bounds, origin: point, recent: [point], phase: 'undecided', pullDirection: null };
}

function sample(gesture: Gesture, point: Point): Point[] {
  return [...gesture.recent, point].filter((p) => point.t - p.t <= RECENT_MS);
}

export function moveGesture(gesture: Gesture, point: Point, edges: Edges): Gesture {
  if (gesture.phase === 'native' || gesture.phase === 'cancelled') return gesture;
  const moved: Gesture = { ...gesture, recent: sample(gesture, point) };
  if (gesture.phase !== 'undecided') return moved;

  const dx = point.x - gesture.origin.x;
  const dy = point.y - gesture.origin.y;
  if (Math.abs(dx) < LOCK_PX && Math.abs(dy) < LOCK_PX) return moved;
  if (Math.abs(dx) >= HORIZONTAL_BIAS * Math.abs(dy)) return { ...moved, phase: 'horizontal' };
  if (Math.abs(dx) > Math.abs(dy)) {
    // Diagonal: not clearly a swipe yet. Wait for it to resolve, then leave it to scrolling.
    return Math.max(Math.abs(dx), Math.abs(dy)) >= GIVE_UP_PX
      ? { ...moved, phase: 'native' }
      : moved;
  }
  if (dy < 0 && edges.atBottom) return { ...moved, phase: 'pull', pullDirection: 'next' };
  if (dy > 0 && edges.atTop) return { ...moved, phase: 'pull', pullDirection: 'previous' };
  return { ...moved, phase: 'native' };
}

/** A second finger or a pointer cancel: nothing commits and nothing is drawn. */
export function cancelGesture(gesture: Gesture): Gesture {
  return { ...gesture, phase: 'cancelled' };
}

function dragX(gesture: Gesture): number {
  const last = gesture.recent[gesture.recent.length - 1] ?? gesture.origin;
  return last.x - gesture.origin.x;
}

function pullTravel(gesture: Gesture): number {
  const last = gesture.recent[gesture.recent.length - 1] ?? gesture.origin;
  const dy = last.y - gesture.origin.y;
  return Math.max(0, gesture.pullDirection === 'next' ? -dy : dy);
}

/**
 * The speed of the last RECENT_MS before `t`, px per ms, positive to the right. A finger that
 * paused before lifting has no recent samples and so no speed.
 */
function velocityX(gesture: Gesture, t: number): number {
  const recent = gesture.recent.filter((p) => t - p.t <= RECENT_MS);
  const first = recent[0];
  const last = recent[recent.length - 1];
  if (!first || !last || last.t === first.t) return 0;
  return (last.x - first.x) / (last.t - first.t);
}

function pushesAgainstEnd(gesture: Gesture, dx: number): 'first' | 'last' | null {
  if (dx > 0 && !gesture.bounds.hasPrevious) return 'first';
  if (dx < 0 && !gesture.bounds.hasNext) return 'last';
  return null;
}

function pullEdge(gesture: Gesture): 'first' | 'last' | null {
  if (gesture.pullDirection === 'next' && !gesture.bounds.hasNext) return 'last';
  if (gesture.pullDirection === 'previous' && !gesture.bounds.hasPrevious) return 'first';
  return null;
}

/** What to draw for the gesture as it stands. */
export function gestureView(gesture: Gesture): GestureView {
  if (gesture.phase === 'horizontal') {
    const dx = dragX(gesture);
    const edge = pushesAgainstEnd(gesture, dx);
    const progress = Math.min(
      1,
      Math.abs(dx) / (Math.max(gesture.bounds.width, 1) * COMMIT_FRACTION),
    );
    if (edge === null) {
      const width = Math.max(gesture.bounds.width, 1);
      const shift = SHIFT_CAP * width * (1 - Math.exp(-Math.abs(dx) / (SHIFT_SCALE * width)));
      return {
        offsetX: (Math.sign(dx) * Math.round(shift * 10)) / 10,
        pull: null,
        edge: null,
        ready: Math.abs(dx) >= width * COMMIT_FRACTION,
        progress,
      };
    }
    const damped = Math.min(Math.abs(dx) * RUBBER, RUBBER_MAX_PX);
    return { offsetX: Math.sign(dx) * damped, pull: null, edge, ready: false, progress };
  }
  if (gesture.phase === 'pull' && gesture.pullDirection !== null) {
    const travel = pullTravel(gesture);
    const edge = pullEdge(gesture);
    return {
      offsetX: 0,
      pull: {
        direction: gesture.pullDirection,
        distance: Math.min(travel * PULL_RESISTANCE, PULL_MAX_PX),
        ready: edge === null && travel >= PULL_THRESHOLD_PX,
        edge,
      },
      edge: null,
      ready: false,
      progress: 0,
    };
  }
  return NOT_ACTIVE;
}

/** The release at time `t` (default: the last sample): which chapter to go to, if any. */
export function endGesture(gesture: Gesture, t?: number): { commit: Commit | null } {
  if (gesture.phase === 'horizontal') {
    const dx = dragX(gesture);
    if (pushesAgainstEnd(gesture, dx) !== null) return { commit: null };
    const far = Math.abs(dx) >= gesture.bounds.width * COMMIT_FRACTION;
    const velocity = velocityX(gesture, t ?? gesture.recent[gesture.recent.length - 1]?.t ?? 0);
    const flicked =
      Math.abs(dx) >= FLICK_MIN_PX &&
      Math.abs(velocity) >= FLICK_SPEED &&
      Math.sign(velocity) === Math.sign(dx);
    if (far || flicked) return { commit: dx < 0 ? 'next' : 'previous' };
  }
  if (gesture.phase === 'pull' && gesture.pullDirection !== null) {
    if (pullEdge(gesture) === null && pullTravel(gesture) >= PULL_THRESHOLD_PX) {
      return { commit: gesture.pullDirection };
    }
  }
  return { commit: null };
}
