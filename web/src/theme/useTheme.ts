import { useCallback, useSyncExternalStore } from 'react';
import { THEME_STORAGE_KEY } from './prepaint';

export type ThemeChoice = 'system' | 'light' | 'dark';

function readStored(): ThemeChoice {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {
    return 'system';
  }
}

function apply(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

// One store for the whole app so every consumer sees the same choice. System leaves
// data-theme unset, and the CSS media query then follows the OS live.
let current: ThemeChoice | undefined;
const listeners = new Set<() => void>();

function getSnapshot(): ThemeChoice {
  current ??= readStored();
  return current;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  apply(getSnapshot());
  return () => {
    listeners.delete(listener);
    // nobody is watching: forget the cache so the next consumer rereads storage
    if (listeners.size === 0) current = undefined;
  };
}

function set(next: ThemeChoice): void {
  current = next;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, next);
  } catch {
    // storage unavailable: the choice lasts until reload
  }
  apply(next);
  listeners.forEach((listener) => {
    listener();
  });
}

/** The theme choice (stored per device), shared by every consumer. */
export function useTheme(): { choice: ThemeChoice; setChoice: (choice: ThemeChoice) => void } {
  const choice = useSyncExternalStore(subscribe, getSnapshot);
  const setChoice = useCallback((next: ThemeChoice) => {
    set(next);
  }, []);
  return { choice, setChoice };
}
