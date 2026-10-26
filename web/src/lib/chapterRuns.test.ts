import { describe, expect, it } from 'vitest';
import { makeChapter, makeChapters } from '../test/fixtures';
import { SEGMENT_COLOR, runsOf } from './chapterRuns';

const all = () => true;

describe('runsOf', () => {
  it('is empty for no chapters', () => {
    expect(runsOf([], all, false)).toEqual([]);
  });

  it('merges consecutive chapters in the same state into one run', () => {
    const chapters = makeChapters(6, { 3: 'error', 4: 'error', 6: 'pending' });
    expect(runsOf(chapters, all, false)).toEqual([
      { state: 'downloaded', matched: true, start: 0, length: 2 },
      { state: 'error', matched: true, start: 2, length: 2 },
      { state: 'downloaded', matched: true, start: 4, length: 1 },
      { state: 'pending', matched: true, start: 5, length: 1 },
    ]);
  });

  it('with split, also breaks a run where the match changes', () => {
    const chapters = [makeChapter(1), makeChapter(2), makeChapter(3)];
    const matched = (c: { position: number }) => c.position !== 2;
    expect(runsOf(chapters, matched, true).map((r) => [r.start, r.length, r.matched])).toEqual([
      [0, 1, true],
      [1, 1, false],
      [2, 1, true],
    ]);
  });

  it('without split, ignores the match', () => {
    const chapters = makeChapters(3);
    expect(runsOf(chapters, () => false, false)).toHaveLength(1);
  });
});

describe('SEGMENT_COLOR', () => {
  it('has a colour for every chapter state', () => {
    expect(Object.keys(SEGMENT_COLOR).sort()).toEqual(
      ['downloaded', 'downloading', 'error', 'not_requested', 'pending'].sort(),
    );
  });
});
