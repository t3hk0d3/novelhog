import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { BrowserRouter, useLocation } from 'react-router';
import { describe, expect, it } from 'vitest';
import { makeChapter, makeChapters } from '../test/fixtures';
import { useSelection } from './useSelection';

const chapters = makeChapters(10);
const positions = (result: { current: ReturnType<typeof useSelection> }) =>
  result.current.selected.map((c) => c.position);

function setup(all = chapters, visible = all) {
  return renderHook(({ a, v }) => useSelection(a, v), { initialProps: { a: all, v: visible } });
}

describe('useSelection', () => {
  describe('enter', () => {
    it('activates select mode with the given chapter selected and announces it', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[2]?.id);
      });
      expect(result.current.active).toBe(true);
      expect(positions(result)).toEqual([3]);
      expect(result.current.announcement).toBe('Select mode, 1 selected');
    });

    it('activates with nothing selected when given no chapter', () => {
      const { result } = setup();
      act(() => {
        result.current.enter();
      });
      expect(positions(result)).toEqual([]);
      expect(result.current.announcement).toBe('Select mode, 0 selected');
    });

    it('pushes a history entry', () => {
      const { result } = setup();
      const before = window.history.length;
      act(() => {
        result.current.enter();
      });
      expect(window.history.length).toBe(before + 1);
      act(() => {
        result.current.exit();
      });
    });
  });

  describe('toggle', () => {
    it('adds and removes a chapter', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[0]?.id);
        result.current.toggle(chapters[4]?.id ?? '');
      });
      expect(positions(result)).toEqual([1, 5]);
      act(() => {
        result.current.toggle(chapters[0]?.id ?? '');
      });
      expect(positions(result)).toEqual([5]);
    });
  });

  describe('rangeTo', () => {
    it('selects from the last selected row to the target, either direction', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[5]?.id);
      });
      act(() => {
        result.current.rangeTo(chapters[2]?.id ?? '');
      });
      expect(positions(result)).toEqual([3, 4, 5, 6]);
    });

    it('selects only the target when nothing was selected before', () => {
      const { result } = setup();
      act(() => {
        result.current.enter();
      });
      act(() => {
        result.current.rangeTo(chapters[3]?.id ?? '');
      });
      expect(positions(result)).toEqual([4]);
    });

    it('covers only visible rows between the ends', () => {
      const visible = chapters.filter((c) => c.position % 2 === 0);
      const { result } = setup(chapters, visible);
      act(() => {
        result.current.enter(chapters[1]?.id);
      });
      act(() => {
        result.current.rangeTo(chapters[7]?.id ?? '');
      });
      expect(positions(result)).toEqual([2, 4, 6, 8]);
    });
  });

  describe('select all and the header checkbox', () => {
    it('selects every visible row, not hidden ones', () => {
      const visible = chapters.slice(0, 3);
      const { result } = setup(chapters, visible);
      act(() => {
        result.current.enter();
      });
      act(() => {
        result.current.selectAll();
      });
      expect(positions(result)).toEqual([1, 2, 3]);
    });

    it('reports none, some and all', () => {
      const { result } = setup(chapters, chapters.slice(0, 2));
      act(() => {
        result.current.enter();
      });
      expect(result.current.visibleState).toBe('none');
      act(() => {
        result.current.toggle(chapters[0]?.id ?? '');
      });
      expect(result.current.visibleState).toBe('some');
      act(() => {
        result.current.toggle(chapters[1]?.id ?? '');
      });
      expect(result.current.visibleState).toBe('all');
    });

    it('toggleAllVisible selects the rest when some, clears visible rows when all', () => {
      const { result } = setup(chapters, chapters.slice(0, 2));
      act(() => {
        result.current.enter(chapters[0]?.id);
      });
      act(() => {
        result.current.toggleAllVisible();
      });
      expect(positions(result)).toEqual([1, 2]);
      act(() => {
        result.current.toggleAllVisible();
      });
      expect(positions(result)).toEqual([]);
    });
  });

  describe('leaving', () => {
    it('exit clears the selection and the announcement', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[0]?.id);
      });
      act(() => {
        result.current.exit();
      });
      expect(result.current.active).toBe(false);
      expect(positions(result)).toEqual([]);
      expect(result.current.announcement).toBe('');
    });

    it('Escape leaves the mode', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[0]?.id);
      });
      act(() => {
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      });
      expect(result.current.active).toBe(false);
    });

    it('Escape is left to an open dialog', () => {
      const { result } = setup();
      const dialog = document.createElement('dialog');
      dialog.setAttribute('open', '');
      document.body.append(dialog);
      act(() => {
        result.current.enter(chapters[0]?.id);
      });
      act(() => {
        document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      });
      expect(result.current.active).toBe(true);
      dialog.remove();
      act(() => {
        result.current.exit();
      });
    });

    it('the back gesture leaves the mode', async () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[0]?.id);
      });
      act(() => {
        window.history.back();
      });
      await waitFor(() => {
        expect(result.current.active).toBe(false);
      });
    });
  });

  describe('live updates', () => {
    it('keeps selected chapters selected when their objects are replaced', () => {
      const { result, rerender } = setup();
      act(() => {
        result.current.enter(chapters[1]?.id);
      });
      const updated = chapters.map((c) =>
        c.position === 2 ? makeChapter(2, { state: 'pending' }) : c,
      );
      rerender({ a: updated, v: updated });
      expect(result.current.selected[0]?.state).toBe('pending');
    });

    it('drops a selected chapter that disappears from the list', () => {
      const { result, rerender } = setup();
      act(() => {
        result.current.enter(chapters[1]?.id);
      });
      const rest = chapters.filter((c) => c.position !== 2);
      rerender({ a: rest, v: rest });
      expect(positions(result)).toEqual([]);
    });
  });

  describe('under a BrowserRouter', () => {
    it('the back gesture leaves the mode and leaves the location alone', async () => {
      window.history.replaceState(null, '', '/novels/NV1?x=1');
      const wrapper = ({ children }: { children: ReactNode }) =>
        createElement(BrowserRouter, null, children);
      const { result } = renderHook(
        () => ({ selection: useSelection(chapters, chapters), location: useLocation() }),
        { wrapper },
      );
      act(() => {
        result.current.selection.enter(chapters[0]?.id);
      });
      act(() => {
        window.history.back();
      });
      await waitFor(() => {
        expect(result.current.selection.active).toBe(false);
      });
      expect(result.current.location.pathname).toBe('/novels/NV1');
      expect(result.current.location.search).toBe('?x=1');
    });
  });

  describe('toggle and the anchor', () => {
    it('a deselected row stops being the anchor for ranges', () => {
      const { result } = setup();
      act(() => {
        result.current.enter(chapters[1]?.id);
      });
      act(() => {
        result.current.toggle(chapters[3]?.id ?? '');
      });
      act(() => {
        result.current.toggle(chapters[3]?.id ?? '');
      });
      act(() => {
        result.current.rangeTo(chapters[5]?.id ?? '');
      });
      expect(positions(result)).toEqual([2, 3, 4, 5, 6]);
    });
  });
});
