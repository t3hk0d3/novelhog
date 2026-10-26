import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MAX_NOVELS, RANGES_KEY, rememberRange, rememberedRange } from './rangeMemory';

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

const stored = () => JSON.parse(localStorage.getItem(RANGES_KEY) ?? 'null') as unknown;

describe('rememberedRange', () => {
  it('knows nothing of a novel it has not seen', () => {
    expect(rememberedRange('NV1')).toBeNull();
  });

  it('gives back the range remembered for a novel, and only for that novel', () => {
    rememberRange('NV1', 3, 1000);
    rememberRange('NV2', 0, 2000);
    expect(rememberedRange('NV1')).toBe(3);
    expect(rememberedRange('NV2')).toBe(0);
    expect(rememberedRange('NV3')).toBeNull();
  });

  it('ignores stored values that are malformed', () => {
    localStorage.setItem(RANGES_KEY, 'not json');
    expect(rememberedRange('NV1')).toBeNull();
    localStorage.setItem(
      RANGES_KEY,
      JSON.stringify({ NV1: 'x', NV2: [-1, 5], NV3: [1.5, 5], NV4: [2] }),
    );
    for (const id of ['NV1', 'NV2', 'NV3', 'NV4']) expect(rememberedRange(id)).toBeNull();
  });
});

describe('rememberRange', () => {
  it('keeps everything under one key', () => {
    rememberRange('NV1', 2, 1000);
    expect(stored()).toEqual({ NV1: [2, 1000] });
  });

  it('replaces the range of a novel', () => {
    rememberRange('NV1', 2, 1000);
    rememberRange('NV1', 0, 2000);
    expect(rememberedRange('NV1')).toBe(0);
  });

  it('keeps only the most recently used novels', () => {
    for (let i = 0; i < MAX_NOVELS + 5; i++) rememberRange(`NV${String(i)}`, 1, i);
    expect(Object.keys(stored() as object)).toHaveLength(MAX_NOVELS);
    expect(rememberedRange('NV0')).toBeNull();
    expect(rememberedRange(`NV${String(MAX_NOVELS + 4)}`)).toBe(1);
  });

  it('survives storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    expect(() => {
      rememberRange('NV1', 2, 1);
    }).not.toThrow();
    expect(rememberedRange('NV1')).toBeNull();
  });
});
