import { useEffect, useState } from 'react';
import { startBar, stepBar } from '../lib/barVisibility';

function furthest(): number {
  return document.documentElement.scrollHeight - window.innerHeight;
}

/**
 * Whether the bottom bar should be slid away: it is while the reader scrolls down and returns on
 * scrolling up (see `lib/barVisibility`). `resetKey` is the chapter: a new one starts with the bar
 * in place and measures from wherever the page is.
 */
export function useBarVisibility(resetKey: string): boolean {
  // The chapter the bar was hidden for: another chapter is a fresh start without any reset.
  const [hiddenFor, setHiddenFor] = useState<string | null>(null);
  useEffect(() => {
    let state = startBar(window.scrollY);
    const onScroll = () => {
      state = stepBar(state, window.scrollY, furthest());
      setHiddenFor(state.hidden ? resetKey : null);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.removeEventListener('scroll', onScroll);
    };
  }, [resetKey]);
  return hiddenFor === resetKey;
}
