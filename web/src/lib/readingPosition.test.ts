import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  MAX_ENTRIES,
  POSITIONS_KEY,
  offsetFor,
  ratioOf,
  savePosition,
  savedRatio,
} from './readingPosition';

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

const stored = () => JSON.parse(localStorage.getItem(POSITIONS_KEY) ?? 'null') as unknown;

describe('ratioOf', () => {
  it('is how far down the scrollable distance the page is', () => {
    expect(ratioOf(500, 2000, 1000)).toBeCloseTo(0.5, 5);
  });

  it('is 0 at the top and 1 at the end, and never outside 0..1 (bounce)', () => {
    expect(ratioOf(0, 2000, 1000)).toBe(0);
    expect(ratioOf(1000, 2000, 1000)).toBe(1);
    expect(ratioOf(-40, 2000, 1000)).toBe(0);
    expect(ratioOf(1200, 2000, 1000)).toBe(1);
  });

  it('is 0 when the page does not scroll', () => {
    expect(ratioOf(0, 800, 1000)).toBe(0);
  });
});

describe('offsetFor', () => {
  it('is the scroll position of a ratio on the current page', () => {
    expect(offsetFor(0.25, 3000, 1000)).toBe(500);
  });

  it('is 0 when the page does not scroll', () => {
    expect(offsetFor(0.5, 800, 1000)).toBe(0);
  });
});

describe('savePosition and savedRatio', () => {
  it('remembers a chapter position under one key', () => {
    savePosition('CH1', 0.4, 1000);
    savePosition('CH2', 0.6, 2000);
    expect(savedRatio('CH1')).toBeCloseTo(0.4, 5);
    expect(savedRatio('CH2')).toBeCloseTo(0.6, 5);
    expect(Object.keys(stored() as object).sort()).toEqual(['CH1', 'CH2']);
  });

  it('knows nothing of a chapter it has not seen', () => {
    expect(savedRatio('CHx')).toBeNull();
  });

  it('forgets a chapter that is back at the top', () => {
    savePosition('CH1', 0.4, 1000);
    savePosition('CH1', 0.005, 2000);
    expect(savedRatio('CH1')).toBeNull();
    expect(stored()).toEqual({});
  });

  it('treats a chapter read to the end as not started: it opens at the top again', () => {
    savePosition('CH1', 0.98, 1000);
    expect(savedRatio('CH1')).toBeNull();
  });

  it('keeps only the most recently read chapters', () => {
    for (let i = 0; i < MAX_ENTRIES + 10; i++) savePosition(`CH${String(i)}`, 0.5, i);
    expect(Object.keys(stored() as object)).toHaveLength(MAX_ENTRIES);
    expect(savedRatio('CH0')).toBeNull();
    expect(savedRatio(`CH${String(MAX_ENTRIES + 9)}`)).not.toBeNull();
  });

  it('ignores a stored value that is malformed', () => {
    localStorage.setItem(POSITIONS_KEY, 'not json');
    expect(savedRatio('CH1')).toBeNull();
    localStorage.setItem(POSITIONS_KEY, JSON.stringify({ CH1: 'x', CH2: [2, 1], CH3: [0.5] }));
    expect(savedRatio('CH1')).toBeNull();
    expect(savedRatio('CH2')).toBeNull();
    expect(savedRatio('CH3')).toBeNull();
  });

  it('survives storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    expect(() => {
      savePosition('CH1', 0.5, 1);
    }).not.toThrow();
    expect(savedRatio('CH1')).toBeNull();
  });
});
