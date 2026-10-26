import { useLayoutEffect } from 'react';
import { offsetFor, ratioOf, savePosition, savedRatio } from '../lib/readingPosition';

/** How long after the last scroll the position is written (ms). */
const SAVE_MS = 400;
/** After the text appears, images that load may push it down; the spot is held for this long (ms). */
const SETTLE_MS = 3000;

/** Input that means the reader has taken over the scrolling. */
const USER_EVENTS = ['touchstart', 'wheel', 'keydown', 'mousedown'] as const;

/**
 * Where the page is scrolled for the open chapter. A new chapter starts at the top; once its text
 * is in (`ready`) the position it was left at, if any, is restored, and held while images load
 * and shift the text, until the reader scrolls. Scrolling is saved a moment after it stops and
 * when the page is hidden or left, keyed by chapter in `lib/readingPosition`.
 *
 * These are layout effects on purpose: the cleanup for the chapter being left must run in the
 * same commit that swaps its text out, before the browser reports the scroll the shorter page
 * causes (that would otherwise be saved as this chapter's position).
 */
export function useReadingPosition(chapterId: string, ready: boolean) {
  useLayoutEffect(() => {
    window.scrollTo(0, 0);
  }, [chapterId]);

  useLayoutEffect(() => {
    if (!ready) return;
    const id = chapterId;
    const target = savedRatio(id);
    let taken = false;
    let last: number | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const apply = () => {
      if (target === null || taken) return;
      const { scrollHeight } = document.documentElement;
      window.scrollTo(0, offsetFor(target, scrollHeight, window.innerHeight));
    };
    const flush = () => {
      clearTimeout(timer);
      timer = undefined;
      if (last !== null) savePosition(id, last);
      last = null;
    };
    const onScroll = () => {
      last = ratioOf(window.scrollY, document.documentElement.scrollHeight, window.innerHeight);
      timer ??= setTimeout(flush, SAVE_MS);
    };
    const onUser = () => {
      taken = true;
    };
    const onImage = (event: Event) => {
      if (event.target instanceof HTMLImageElement) apply();
    };
    const onHidden = () => {
      if (document.visibilityState === 'hidden') flush();
    };

    apply();
    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', onHidden);
    for (const name of USER_EVENTS) window.addEventListener(name, onUser, { passive: true });
    document.addEventListener('load', onImage, true);
    const settled = setTimeout(() => {
      document.removeEventListener('load', onImage, true);
    }, SETTLE_MS);

    return () => {
      clearTimeout(settled);
      document.removeEventListener('load', onImage, true);
      document.removeEventListener('visibilitychange', onHidden);
      window.removeEventListener('scroll', onScroll);
      window.removeEventListener('pagehide', flush);
      for (const name of USER_EVENTS) window.removeEventListener(name, onUser);
      flush();
    };
  }, [chapterId, ready]);
}
