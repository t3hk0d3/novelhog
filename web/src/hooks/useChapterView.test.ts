import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeChapter, makeChapters } from '../test/fixtures';
import { useChapterView } from './useChapterView';

describe('useChapterView', () => {
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

    it('selects the range holding the highest position by default', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      expect(result.current.range).toBe(2);
      expect(result.current.rangeChapters.map((c) => c.position)).toEqual(
        Array.from({ length: 30 }, (_, i) => 101 + i),
      );
    });

    it('keeps an explicitly chosen range', () => {
      const { result } = renderHook(() => useChapterView(makeChapters(130), 0));
      act(() => {
        result.current.setRange(0);
      });
      expect(result.current.range).toBe(0);
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
