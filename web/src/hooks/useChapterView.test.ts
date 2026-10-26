import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { RANGES_KEY } from '../lib/rangeMemory';
import { makeChapter, makeChapters } from '../test/fixtures';
import { useChapterView } from './useChapterView';

describe('useChapterView', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  describe('ranges', () => {
    it('splits positions into ranges of 50, the last one ending at the highest position', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      expect(result.current.ranges.map((r) => [r.from, r.to])).toEqual([
        [1, 50],
        [51, 100],
        [101, 130],
      ]);
    });

    it('counts states per range', () => {
      const chapters = makeChapters(60, { 2: 'error', 55: 'pending' });
      const { result } = renderHook(() => useChapterView(chapters, 0));
      expect(result.current.ranges[0]?.counts).toMatchObject({ error: 1, downloaded: 49 });
      expect(result.current.ranges[1]?.counts).toMatchObject({ pending: 1, downloaded: 9 });
    });

    it('selects the first range by default, not the latest', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      expect(result.current.range).toBe(0);
      expect(result.current.rangeChapters.map((c) => c.position)).toEqual(
        Array.from({ length: 50 }, (_, i) => 1 + i),
      );
    });

    it('keeps an explicitly chosen range', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      act(() => {
        result.current.setRange(0);
      });
      expect(result.current.range).toBe(0);
    });

    describe('remembering the range per novel', () => {
      const open = (novelId: string, chapters = makeChapters(130)) =>
        renderHook(() => useChapterView(chapters, 0, novelId));

      it('starts the next visit on the range that was chosen last', () => {
        const first = open('NV1');
        act(() => {
          first.result.current.setRange(2);
        });
        first.unmount();
        const second = open('NV1');
        expect(second.result.current.range).toBe(2);
        expect(second.result.current.rangeChapters[0]?.position).toBe(101);
      });

      it('keeps the latest choice', () => {
        const first = open('NV1');
        act(() => {
          first.result.current.setRange(2);
        });
        act(() => {
          first.result.current.setRange(1);
        });
        first.unmount();
        expect(open('NV1').result.current.range).toBe(1);
      });

      it('is per novel: another novel still starts on its first range', () => {
        const first = open('NV1');
        act(() => {
          first.result.current.setRange(2);
        });
        expect(open('NV2').result.current.range).toBe(0);
      });

      it('falls back to the first range when the remembered one no longer exists', () => {
        const long = open('NV1');
        act(() => {
          long.result.current.setRange(2);
        });
        long.unmount();
        expect(open('NV1', makeChapters(60)).result.current.range).toBe(0);
      });

      it('remembers nothing for a view without a novel', () => {
        const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
        act(() => {
          result.current.setRange(2);
        });
        expect(localStorage.getItem(RANGES_KEY)).toBeNull();
      });

      it('does not remember a range that was never chosen', () => {
        open('NV1');
        expect(localStorage.getItem(RANGES_KEY)).toBeNull();
      });
    });

    it('has no ranges and no selected range without chapters', () => {
      const { result } = renderHook(() => useChapterView([], 0));
      expect(result.current.ranges).toEqual([]);
      expect(result.current.range).toBeNull();
    });
  });

  describe('search', () => {
    it('matches a chapter number exactly', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      act(() => {
        result.current.setQuery('#12');
      });
      expect(result.current.filterActive).toBe(true);
      expect(result.current.matches.map((c) => c.position)).toEqual([12]);
    });

    it('matches a title case-insensitively', () => {
      const chapters = [makeChapter(1, { title: 'The Storm' }), makeChapter(2, { title: null })];
      const { result } = renderHook(() => useChapterView(chapters, 0));
      act(() => {
        result.current.setQuery(' storm ');
      });
      expect(result.current.matches.map((c) => c.position)).toEqual([1]);
    });
  });

  describe('state filters', () => {
    it('keeps chapters in any of the chosen states and toggles off again', () => {
      const chapters = makeChapters(10, { 3: 'error', 4: 'pending' });
      const { result } = renderHook(() => useChapterView(chapters, 0));
      act(() => {
        result.current.toggleState('error');
        result.current.toggleState('pending');
      });
      expect(result.current.matches.map((c) => c.position)).toEqual([3, 4]);
      act(() => {
        result.current.toggleState('error');
        result.current.toggleState('pending');
      });
      expect(result.current.filterActive).toBe(false);
    });

    it('counts all chapters per state', () => {
      const chapters = makeChapters(10, { 3: 'error' });
      const { result } = renderHook(() => useChapterView(chapters, 0));
      expect(result.current.stateCounts).toMatchObject({ error: 1, downloaded: 9, pending: 0 });
    });
  });

  describe('new filter', () => {
    it('marks the given number of most recently released chapters as new', () => {
      const chapters = [
        makeChapter(1, { released_at: '2026-10-01T00:00:00.000Z' }),
        makeChapter(2, { released_at: '2026-10-05T00:00:00.000Z' }),
        makeChapter(3, { released_at: '2026-10-05T00:00:00.000Z' }),
        makeChapter(4, { released_at: null }),
      ];
      const { result } = renderHook(() => useChapterView(chapters, 2));
      expect(result.current.newCount).toBe(2);
      act(() => {
        result.current.toggleNew();
      });
      expect(result.current.matches.map((c) => c.position)).toEqual([2, 3]);
    });

    it('keeps older new chapters new and adds one released later, even with none new at open', () => {
      const at = (day: string) => `2026-10-${day}T00:00:00.000Z`;
      const initial = [
        makeChapter(1, { released_at: at('01') }),
        makeChapter(2, { released_at: at('05') }),
        makeChapter(3, { released_at: at('06') }),
      ];
      const added = [...initial, makeChapter(4, { released_at: at('09') })];
      for (const [newCount, expected] of [
        [2, [2, 3, 4]],
        [0, [4]],
      ] as const) {
        const { result, rerender } = renderHook(({ list }) => useChapterView(list, newCount), {
          initialProps: { list: initial },
        });
        rerender({ list: added });
        expect(result.current.newCount).toBe(expected.length);
        expect(added.filter(result.current.isNew).map((c) => c.position)).toEqual(expected);
      }
    });

    it('finds nothing new when no chapter was released since the last visit', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(5), 0));
      expect(result.current.newCount).toBe(0);
    });
  });

  describe('results', () => {
    it('groups matches from all ranges under their range', () => {
      const chapters = makeChapters(130, { 7: 'error', 120: 'error' });
      const { result } = renderHook(() => useChapterView(chapters, 0));
      act(() => {
        result.current.toggleState('error');
      });
      expect(
        result.current.groups.map((g) => [g.range.from, g.chapters.map((c) => c.position)]),
      ).toEqual([
        [1, [7]],
        [101, [120]],
      ]);
    });
  });
});
