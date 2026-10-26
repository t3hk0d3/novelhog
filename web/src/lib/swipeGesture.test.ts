import { describe, expect, it } from 'vitest';
import {
  cancelGesture,
  endGesture,
  gestureView,
  moveGesture,
  startGesture,
  type Bounds,
  type Edges,
  type Gesture,
  type Point,
} from './swipeGesture';

const BOUNDS: Bounds = { width: 400, viewportWidth: 400, hasPrevious: true, hasNext: true };
const MIDDLE: Edges = { atTop: false, atBottom: false };
const START: Point = { x: 200, y: 300, t: 0 };

/** Starts at START and moves through `points`, each `[dx, dy, t]` from START. */
function drag(
  moves: [number, number, number][],
  { bounds = BOUNDS, edges = MIDDLE }: { bounds?: Bounds; edges?: Edges } = {},
): Gesture {
  let gesture = startGesture(bounds, START);
  if (gesture === null) throw new Error('gesture did not start');
  for (const [dx, dy, t] of moves) {
    gesture = moveGesture(gesture, { x: START.x + dx, y: START.y + dy, t }, edges);
  }
  return gesture;
}

describe('swipeGesture', () => {
  describe('horizontal drag past 60% of the width and released', () => {
    it('commits to next when dragged left', () => {
      expect(endGesture(drag([[-260, 0, 900]])).commit).toBe('next');
    });

    it('commits to previous when dragged right', () => {
      expect(endGesture(drag([[260, 0, 900]])).commit).toBe('previous');
    });
  });

  describe('the column while dragging', () => {
    it('moves less than the finger, and ever less the further it goes (resistance)', () => {
      const at = (dx: number) => Math.abs(gestureView(drag([[-dx, 3, 300]])).offsetX);
      expect(at(60)).toBeGreaterThan(30);
      expect(at(60)).toBeLessThan(60);
      expect(at(240) - at(120)).toBeLessThan(at(120) - at(0));
    });

    it('never moves past 40% of the width, however far the finger goes', () => {
      expect(Math.abs(gestureView(drag([[-1500, 3, 900]])).offsetX)).toBeLessThanOrEqual(160);
    });

    it('is about 28% of the width where a release commits', () => {
      const offset = Math.abs(gestureView(drag([[-240, 3, 900]])).offsetX);
      expect(offset / 400).toBeGreaterThan(0.25);
      expect(offset / 400).toBeLessThan(0.3);
    });

    it('has a progress from 0 to 1 over the drag that a release needs', () => {
      expect(gestureView(drag([[-60, 3, 300]])).progress).toBeCloseTo(0.25, 5);
      expect(gestureView(drag([[-120, 3, 300]])).progress).toBeCloseTo(0.5, 5);
      expect(gestureView(drag([[-240, 3, 900]])).progress).toBeCloseTo(1, 5);
      expect(gestureView(drag([[-900, 3, 900]])).progress).toBe(1);
    });

    it('has the same progress at the end of the book, where nothing will happen', () => {
      const last = { ...BOUNDS, hasNext: false };
      expect(gestureView(drag([[-120, 3, 300]], { bounds: last })).progress).toBeCloseTo(0.5, 5);
    });

    it('is ready to commit once the drag is long enough, and not before', () => {
      expect(gestureView(drag([[-200, 0, 900]])).ready).toBe(false);
      expect(gestureView(drag([[-250, 0, 900]])).ready).toBe(true);
    });
  });

  describe('a long but not long enough drag, released slowly', () => {
    it('springs back: a chapter change needs a very long drag or a hard flick', () => {
      expect(endGesture(drag([[-120, 0, 900]])).commit).toBeNull();
      expect(endGesture(drag([[-200, 0, 900]])).commit).toBeNull();
      expect(endGesture(drag([[200, 0, 900]])).commit).toBeNull();
    });
  });

  describe('fast flick', () => {
    it('commits although the distance is shorter', () => {
      expect(endGesture(drag([[-120, 0, 100]])).commit).toBe('next');
    });

    it('does not commit when it is quick but tiny', () => {
      expect(endGesture(drag([[-50, 0, 40]])).commit).toBeNull();
    });

    it('does not commit when it is long but not hard enough', () => {
      expect(endGesture(drag([[-120, 0, 250]])).commit).toBeNull();
    });

    it('does not commit when the finger came back at the end', () => {
      expect(
        endGesture(
          drag([
            [-50, 0, 300],
            [-45, 0, 330],
            [-30, 0, 360],
          ]),
        ).commit,
      ).toBeNull();
    });
  });

  describe('flick followed by a pause before the finger lifts', () => {
    it('does not commit: the speed is that of the moment of release', () => {
      const gesture = drag([[-120, 0, 100]]);
      expect(endGesture(gesture, 100).commit).toBe('next');
      expect(endGesture(gesture, 700).commit).toBeNull();
    });
  });

  describe('slow short drag', () => {
    it('cancels and springs back', () => {
      const ended = endGesture(drag([[-50, 0, 900]]));
      expect(ended.commit).toBeNull();
    });
  });

  describe('movement mostly vertical in the first 10px', () => {
    it('is released to native scrolling and never commits', () => {
      const gesture = drag([
        [2, 12, 50],
        [-200, 40, 400],
      ]);
      expect(gestureView(gesture)).toEqual({
        offsetX: 0,
        pull: null,
        edge: null,
        ready: false,
        progress: 0,
      });
      expect(endGesture(gesture).commit).toBeNull();
    });
  });

  describe('drag on the first chapter to the right / on the last chapter to the left', () => {
    const first = { ...BOUNDS, hasPrevious: false };
    const last = { ...BOUNDS, hasNext: false };

    it('damps the offset to a fraction of the drag', () => {
      const offset = gestureView(drag([[100, 0, 300]], { bounds: first })).offsetX;
      expect(offset).toBeGreaterThan(0);
      expect(offset).toBeLessThan(50);
    });

    it('does not commit on release', () => {
      expect(endGesture(drag([[150, 0, 100]], { bounds: first })).commit).toBeNull();
      expect(endGesture(drag([[-150, 0, 100]], { bounds: last })).commit).toBeNull();
    });

    it('reports the edge so the hint can be shown', () => {
      expect(gestureView(drag([[100, 0, 300]], { bounds: first })).edge).toBe('first');
      expect(gestureView(drag([[-100, 0, 300]], { bounds: last })).edge).toBe('last');
    });

    it('still commits the other way', () => {
      expect(endGesture(drag([[-300, 0, 300]], { bounds: first })).commit).toBe('next');
    });
  });

  describe('a diagonal movement', () => {
    it('is not a swipe while it is still ambiguous, and never commits', () => {
      const gesture = drag([[22, 18, 100]]);
      expect(gestureView(gesture)).toEqual({
        offsetX: 0,
        pull: null,
        edge: null,
        ready: false,
        progress: 0,
      });
      expect(endGesture(gesture).commit).toBeNull();
    });

    it('becomes a swipe once it is clearly sideways', () => {
      const gesture = drag([
        [22, 18, 100],
        [90, 30, 200],
      ]);
      expect(gestureView(gesture).offsetX).toBeCloseTo(57.9, 0);
    });

    it('is given to native scrolling when it stays diagonal', () => {
      const gesture = drag([
        [22, 18, 100],
        [45, 38, 200],
        [-300, 40, 400],
      ]);
      expect(gestureView(gesture).offsetX).toBe(0);
      expect(endGesture(gesture).commit).toBeNull();
    });
  });

  describe('start within the edge-exclusion zone of the screen', () => {
    it('is ignored', () => {
      expect(startGesture(BOUNDS, { x: 10, y: 300, t: 0 })).toBeNull();
      expect(startGesture(BOUNDS, { x: 392, y: 300, t: 0 })).toBeNull();
    });
  });

  describe('second finger added', () => {
    it('cancels without committing', () => {
      const cancelled = cancelGesture(drag([[-150, 0, 300]]));
      expect(endGesture(cancelled).commit).toBeNull();
      expect(gestureView(cancelled).offsetX).toBe(0);
    });
  });

  describe('pointer cancelled', () => {
    it('cancels without committing', () => {
      const cancelled = cancelGesture(
        drag([[0, -150, 300]], { edges: { atTop: false, atBottom: true } }),
      );
      expect(endGesture(cancelled).commit).toBeNull();
      expect(gestureView(cancelled).pull).toBeNull();
    });
  });

  describe('pull up at the bottom of the text', () => {
    const bottom: Edges = { atTop: false, atBottom: true };

    it('commits to next when released past the threshold', () => {
      const gesture = drag([[0, -130, 500]], { edges: bottom });
      expect(gestureView(gesture).pull).toMatchObject({ direction: 'next', ready: true });
      expect(endGesture(gesture).commit).toBe('next');
    });

    it('cancels when released before the threshold', () => {
      const gesture = drag([[0, -50, 500]], { edges: bottom });
      expect(gestureView(gesture).pull).toMatchObject({ direction: 'next', ready: false });
      expect(endGesture(gesture).commit).toBeNull();
    });

    it('reveals a resisted distance', () => {
      const pull = gestureView(drag([[0, -60, 500]], { edges: bottom })).pull;
      expect(pull?.distance).toBeGreaterThan(0);
      expect(pull?.distance).toBeLessThan(60);
    });
  });

  describe('pull down at the top of the text', () => {
    it('commits to previous when released past the threshold', () => {
      const gesture = drag([[0, 130, 500]], { edges: { atTop: true, atBottom: false } });
      expect(endGesture(gesture).commit).toBe('previous');
    });
  });

  describe('pull at the bottom of the last chapter (or the top of the first)', () => {
    it('never commits and reports the edge', () => {
      const last = drag([[0, -200, 500]], {
        bounds: { ...BOUNDS, hasNext: false },
        edges: { atTop: false, atBottom: true },
      });
      expect(endGesture(last).commit).toBeNull();
      expect(gestureView(last).pull).toMatchObject({ direction: 'next', edge: 'last' });

      const first = drag([[0, 200, 500]], {
        bounds: { ...BOUNDS, hasPrevious: false },
        edges: { atTop: true, atBottom: false },
      });
      expect(endGesture(first).commit).toBeNull();
      expect(gestureView(first).pull).toMatchObject({ direction: 'previous', edge: 'first' });
    });
  });

  describe('pull while not at the end of the text', () => {
    it('is ignored (native scroll)', () => {
      const gesture = drag([[0, -200, 500]]);
      expect(gestureView(gesture).pull).toBeNull();
      expect(endGesture(gesture).commit).toBeNull();
    });

    it('is ignored when pulling away from the edge the page is at', () => {
      const gesture = drag([[0, 200, 500]], { edges: { atTop: false, atBottom: true } });
      expect(gestureView(gesture).pull).toBeNull();
    });
  });
});
