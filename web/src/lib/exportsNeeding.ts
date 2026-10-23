import type { Export } from '../api/client';

/**
 * The waiting exports that cannot finish without the chapters at `positions`: not `skip_missing`
 * and with the position inside their range. Cancelling those chapters fails these exports.
 */
export function exportsNeeding(exports: Export[] | undefined, positions: number[]): Export[] {
  return (exports ?? []).filter(
    (e) =>
      e.state === 'waiting' &&
      !e.skip_missing &&
      e.chapter_from !== null &&
      e.chapter_to !== null &&
      positions.some((p) => p >= (e.chapter_from ?? 0) && p <= (e.chapter_to ?? 0)),
  );
}
