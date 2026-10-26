import { useEffect, useRef, useState } from 'react';
import {
  cancelGesture,
  endGesture,
  gestureView,
  moveGesture,
  startGesture,
  type Commit,
  type Edges,
  type Gesture,
  type GestureView,
} from '../lib/swipeGesture';

/** How long the column takes to slide out after a committed swipe (ms). */
export const EXIT_MS = 160;
/** How long the new chapter's slide-in is flagged (ms); the animation itself is CSS. */
const ENTER_MS = 300;

/** How close to the end of the page counts as being at it (fractional scroll positions, toolbars). */
const EDGE_SLACK_PX = 3;

const IDLE: GestureView = { offsetX: 0, pull: null, edge: null, ready: false, progress: 0 };

interface Options {
  hasPrevious: boolean;
  hasNext: boolean;
  /** No sliding: a gesture commits or cancels at once. */
  reducedMotion: boolean;
  /** The release committed to this chapter (after the slide-out, unless motion is reduced). */
  onCommit: (commit: Commit) => void;
}

/**
 * Touch gestures of the reader on the element behind `ref`: a horizontal swipe that follows the
 * finger and a vertical pull past either end of the text (see `lib/swipeGesture`). Touch events
 * are used, not pointer events, because the browser cancels pointer streams as soon as it starts
 * to scroll, and the pull has to be tracked while the page is already at its end. `touchmove` is
 * non-passive so a locked gesture can stop native scrolling and the browser's own overscroll.
 */
export function useChapterGestures(options: Options) {
  // The surface only exists once the chapter has loaded, so it is state: the listeners attach
  // whenever it appears, not just on the first render.
  const [surface, setSurface] = useState<HTMLDivElement | null>(null);
  const latest = useRef(options);
  const [view, setView] = useState<GestureView>(IDLE);
  const [dragging, setDragging] = useState(false);
  const [exiting, setExiting] = useState<Commit | null>(null);
  const [entering, setEntering] = useState<Commit | null>(null);

  useEffect(() => {
    latest.current = options;
  });

  useEffect(() => {
    if (!surface) return;
    const element = surface;
    let gesture: Gesture | null = null;
    let busy = false;
    const timers: ReturnType<typeof setTimeout>[] = [];

    const edges = (): Edges => ({
      atTop: window.scrollY <= 0,
      atBottom:
        window.scrollY + window.innerHeight >=
        document.documentElement.scrollHeight - EDGE_SLACK_PX,
    });

    function reset() {
      gesture = null;
      setView(IDLE);
      setDragging(false);
    }

    function abandon() {
      if (gesture) gesture = cancelGesture(gesture);
      reset();
    }

    function onStart(event: TouchEvent) {
      if (busy) return;
      if (event.touches.length > 1) {
        abandon();
        return;
      }
      const touch = event.touches[0];
      if (!touch) return;
      gesture = startGesture(
        {
          width: element.clientWidth,
          viewportWidth: window.innerWidth,
          hasPrevious: latest.current.hasPrevious,
          hasNext: latest.current.hasNext,
        },
        { x: touch.clientX, y: touch.clientY, t: performance.now() },
      );
    }

    function onMove(event: TouchEvent) {
      if (!gesture || busy) return;
      if (event.touches.length > 1) {
        abandon();
        return;
      }
      const touch = event.touches[0];
      if (!touch) return;
      gesture = moveGesture(
        gesture,
        { x: touch.clientX, y: touch.clientY, t: performance.now() },
        edges(),
      );
      const ours = gesture.phase === 'horizontal' || gesture.phase === 'pull';
      if (ours && event.cancelable) event.preventDefault();
      setDragging(ours);
      setView(gestureView(gesture));
    }

    function onEnd() {
      if (!gesture) return;
      const finished = gesture;
      gesture = null;
      const { commit } = endGesture(finished, performance.now());
      if (commit === null) {
        reset();
        return;
      }
      const slides = finished.phase === 'horizontal' && !latest.current.reducedMotion;
      if (!slides) {
        reset();
        latest.current.onCommit(commit);
        return;
      }
      busy = true;
      setDragging(false);
      setExiting(commit);
      timers.push(
        setTimeout(() => {
          busy = false;
          setExiting(null);
          setView(IDLE);
          setEntering(commit);
          latest.current.onCommit(commit);
          timers.push(
            setTimeout(() => {
              setEntering(null);
            }, ENTER_MS),
          );
        }, EXIT_MS),
      );
    }

    surface.addEventListener('touchstart', onStart, { passive: true });
    surface.addEventListener('touchmove', onMove, { passive: false });
    surface.addEventListener('touchend', onEnd);
    surface.addEventListener('touchcancel', abandon);
    return () => {
      surface.removeEventListener('touchstart', onStart);
      surface.removeEventListener('touchmove', onMove);
      surface.removeEventListener('touchend', onEnd);
      surface.removeEventListener('touchcancel', abandon);
      timers.forEach(clearTimeout);
    };
  }, [surface]);

  return { ref: setSurface, view, dragging, exiting, entering };
}
