import { describe, expect, it } from 'vitest';
import type { Export } from '../api/client';
import { exportsNeeding } from './exportsNeeding';

const make = (overrides: Partial<Export> = {}): Export =>
  ({
    id: 'EX000000000001',
    filename: 'a.epub',
    chapter_from: 5,
    chapter_to: 10,
    skip_missing: false,
    state: 'waiting',
    ...overrides,
  }) as Export;

describe('exportsNeeding', () => {
  it('returns waiting exports whose range contains a position', () => {
    expect(exportsNeeding([make()], [3, 7])).toHaveLength(1);
  });

  it('ignores exports outside the range, skip_missing, not waiting, or with an empty range', () => {
    expect(exportsNeeding([make()], [11])).toEqual([]);
    expect(exportsNeeding([make({ skip_missing: true })], [7])).toEqual([]);
    expect(exportsNeeding([make({ state: 'ready' })], [7])).toEqual([]);
    expect(exportsNeeding([make({ chapter_from: null, chapter_to: null })], [7])).toEqual([]);
  });

  it('copes with no export data yet', () => {
    expect(exportsNeeding(undefined, [7])).toEqual([]);
  });
});
