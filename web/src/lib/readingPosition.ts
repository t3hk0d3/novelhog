/**
 * Where the reader had got to in each chapter, kept in `localStorage` as a ratio of the page's
 * scrollable distance (so it survives a change of text size or window width). Chapters read to
 * the end or never scrolled are not kept; the oldest are dropped past MAX_ENTRIES.
 */

export const POSITIONS_KEY = 'novelhog.reader.positions';
export const MAX_ENTRIES = 300;
/** At or past this the chapter counts as read: it opens at the top next time. */
const FINISHED = 0.97;
/** Below this the chapter counts as not started. */
const STARTED = 0.02;

/** chapter id -> [ratio, time it was saved (ms)] */
type Positions = Record<string, [number, number]>;

function load(): Positions {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(POSITIONS_KEY) ?? 'null');
    if (typeof stored !== 'object' || stored === null || Array.isArray(stored)) return {};
    const positions: Positions = {};
    for (const [id, value] of Object.entries(stored)) {
      if (!Array.isArray(value) || value.length !== 2) continue;
      const [ratio, at] = value as unknown[];
      if (typeof ratio !== 'number' || typeof at !== 'number') continue;
      if (!(ratio >= 0 && ratio <= 1) || !Number.isFinite(at)) continue;
      positions[id] = [ratio, at];
    }
    return positions;
  } catch {
    return {};
  }
}

function store(positions: Positions) {
  try {
    localStorage.setItem(POSITIONS_KEY, JSON.stringify(positions));
  } catch {
    // Without storage nothing is remembered.
  }
}

/** The ratio to reopen `chapterId` at, or null to open it at the top. */
export function savedRatio(chapterId: string): number | null {
  const entry = load()[chapterId];
  if (!entry) return null;
  const [ratio] = entry;
  return ratio >= STARTED && ratio < FINISHED ? ratio : null;
}

export function savePosition(chapterId: string, ratio: number, now: number = Date.now()): void {
  const positions = load();
  if (ratio < STARTED) {
    Reflect.deleteProperty(positions, chapterId);
  } else {
    positions[chapterId] = [Math.round(ratio * 10_000) / 10_000, now];
  }
  const ids = Object.keys(positions);
  if (ids.length > MAX_ENTRIES) {
    ids
      .sort((a, b) => (positions[a]?.[1] ?? 0) - (positions[b]?.[1] ?? 0))
      .slice(0, ids.length - MAX_ENTRIES)
      .forEach((id) => Reflect.deleteProperty(positions, id));
  }
  store(positions);
}

/** How far down the scrollable distance the page is, 0..1 (0 when it does not scroll). */
export function ratioOf(scrollY: number, scrollHeight: number, viewportHeight: number): number {
  const distance = scrollHeight - viewportHeight;
  if (distance <= 0) return 0;
  return Math.min(1, Math.max(0, scrollY / distance));
}

/** The scroll position of `ratio` on a page with this height. */
export function offsetFor(ratio: number, scrollHeight: number, viewportHeight: number): number {
  const distance = scrollHeight - viewportHeight;
  return distance <= 0 ? 0 : Math.round(ratio * distance);
}
