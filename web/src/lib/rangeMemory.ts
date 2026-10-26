/**
 * Which range of chapters (the tabs of 50) the novel page showed last, per novel, kept in
 * `localStorage`: a visit starts where the last one left off. The most recently used
 * MAX_NOVELS novels are kept.
 */

export const RANGES_KEY = 'novelhog.novel-ranges';
export const MAX_NOVELS = 200;

/** novel id -> [range index, time it was chosen (ms)] */
type Ranges = Record<string, [number, number]>;

function load(): Ranges {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(RANGES_KEY) ?? 'null');
    if (typeof stored !== 'object' || stored === null || Array.isArray(stored)) return {};
    const ranges: Ranges = {};
    for (const [id, value] of Object.entries(stored)) {
      if (!Array.isArray(value) || value.length !== 2) continue;
      const [index, at] = value as unknown[];
      if (typeof index !== 'number' || !Number.isInteger(index) || index < 0) continue;
      if (typeof at !== 'number' || !Number.isFinite(at)) continue;
      ranges[id] = [index, at];
    }
    return ranges;
  } catch {
    return {};
  }
}

/** The range index the novel page showed last for `novelId`, or null. */
export function rememberedRange(novelId: string): number | null {
  return load()[novelId]?.[0] ?? null;
}

export function rememberRange(novelId: string, index: number, now: number = Date.now()): void {
  const ranges = load();
  ranges[novelId] = [index, now];
  const ids = Object.keys(ranges);
  if (ids.length > MAX_NOVELS) {
    ids
      .sort((a, b) => (ranges[a]?.[1] ?? 0) - (ranges[b]?.[1] ?? 0))
      .slice(0, ids.length - MAX_NOVELS)
      .forEach((id) => Reflect.deleteProperty(ranges, id));
  }
  try {
    localStorage.setItem(RANGES_KEY, JSON.stringify(ranges));
  } catch {
    // Without storage nothing is remembered.
  }
}
