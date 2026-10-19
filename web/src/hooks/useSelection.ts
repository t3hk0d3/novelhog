import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Chapter } from '../api/client';

export type VisibleState = 'none' | 'some' | 'all';

/**
 * Select mode for the chapter table (spec section 10). `chapters` is the novel's whole list;
 * `visible` the rows currently shown (search and filter included), which "select all", the header
 * checkbox and range selection work on. Selection is kept as ids, so a live update that replaces a
 * selected chapter keeps it selected, and `selected` always reflects the current cached objects.
 * Entering the mode pushes a history entry so the back gesture leaves it; Escape leaves it too
 * unless a dialog is open.
 */
export function useSelection(chapters: Chapter[], visible: Chapter[]) {
  const [active, setActive] = useState(false);
  const [ids, setIds] = useState<ReadonlySet<string>>(new Set());
  const [anchor, setAnchor] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');
  // Whether our history entry is on the stack, and whether the next popstate is our own back().
  const pushed = useRef(false);
  const ownPop = useRef(false);

  const selected = useMemo(() => chapters.filter((c) => ids.has(c.id)), [chapters, ids]);
  const selectedVisible = visible.reduce((n, c) => n + (ids.has(c.id) ? 1 : 0), 0);
  const visibleState: VisibleState =
    selectedVisible === 0 ? 'none' : selectedVisible === visible.length ? 'all' : 'some';

  const leave = useCallback(() => {
    setActive(false);
    setIds(new Set());
    setAnchor(null);
    setAnnouncement('');
  }, []);

  const enter = useCallback((id?: string) => {
    setActive(true);
    setIds(new Set(id === undefined ? [] : [id]));
    setAnchor(id ?? null);
    setAnnouncement(`Select mode, ${id === undefined ? '0' : '1'} selected`);
    if (!pushed.current) {
      pushed.current = true;
      window.history.pushState(window.history.state, '');
    }
  }, []);

  const exit = useCallback(() => {
    leave();
    if (pushed.current) {
      pushed.current = false;
      ownPop.current = true;
      window.history.back();
    }
  }, [leave]);

  useEffect(() => {
    const onPop = () => {
      if (ownPop.current) {
        ownPop.current = false;
        return;
      }
      if (pushed.current) {
        pushed.current = false;
        leave();
      }
    };
    window.addEventListener('popstate', onPop);
    return () => {
      window.removeEventListener('popstate', onPop);
    };
  }, [leave]);

  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      if (document.querySelector('dialog[open]')) return;
      exit();
    };
    // Capture phase: it must see Escape before the dialog's own handling closes the dialog.
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('keydown', onKey, true);
    };
  }, [active, exit]);

  const toggle = useCallback((id: string) => {
    setIds((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
    setAnchor(id);
  }, []);

  /** Adds the visible rows between the last selected row and `id` (just `id` without one). */
  const rangeTo = useCallback(
    (id: string) => {
      const target = chapters.find((c) => c.id === id);
      if (!target) return;
      const from = chapters.find((c) => c.id === anchor)?.position ?? target.position;
      const lo = Math.min(from, target.position);
      const hi = Math.max(from, target.position);
      setIds((current) => {
        const next = new Set(current);
        next.add(id);
        for (const c of visible) if (c.position >= lo && c.position <= hi) next.add(c.id);
        return next;
      });
      setAnchor(id);
    },
    [chapters, visible, anchor],
  );

  const selectAll = useCallback(() => {
    setIds((current) => new Set([...current, ...visible.map((c) => c.id)]));
  }, [visible]);

  /** The header checkbox: clears the visible rows when all are selected, else selects them all. */
  const toggleAllVisible = useCallback(() => {
    if (visibleState === 'all') {
      const shown = new Set(visible.map((c) => c.id));
      setIds((current) => new Set([...current].filter((id) => !shown.has(id))));
    } else selectAll();
  }, [visible, visibleState, selectAll]);

  return {
    active,
    selected,
    isSelected: (id: string) => ids.has(id),
    visibleState,
    announcement,
    enter,
    exit,
    toggle,
    rangeTo,
    selectAll,
    toggleAllVisible,
  };
}

export type Selection = ReturnType<typeof useSelection>;
