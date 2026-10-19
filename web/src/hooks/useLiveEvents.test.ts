import { QueryClient } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { StrictMode, createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter, Export, Novel } from '../api/client';
import { isDeletedNovel } from '../api/events';
import { FakeEventSource } from '../test/FakeEventSource';
import { useLiveEvents, useLiveReady } from './useLiveEvents';

let frames = new Map<number, FrameRequestCallback>();
let nextFrame = 1;
function runFrame() {
  const pending = [...frames.values()];
  frames.clear();
  act(() => {
    pending.forEach((callback) => {
      callback(0);
    });
  });
}

function novel(id: string, extra: Partial<Novel> = {}): Novel {
  return { id, title: id, downloaded_pages: 0, ...extra } as Novel;
}
function chapter(id: string, position: number, extra: Partial<Chapter> = {}): Chapter {
  return { id, position, state: 'not_requested', ...extra } as Chapter;
}
function exportOf(id: string, extra: Partial<Export> = {}): Export {
  return { id, novel_id: 'NV1', state: 'waiting', ...extra } as Export;
}

let client: QueryClient;
let source: FakeEventSource;

function mount() {
  const hook = renderHook(() => {
    useLiveEvents(client);
    return useLiveReady();
  });
  source = FakeEventSource.instances[0] as FakeEventSource;
  return hook;
}

beforeEach(() => {
  FakeEventSource.instances = [];
  frames = new Map();
  client = new QueryClient();
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    frames.set(nextFrame, callback);
    return nextFrame++;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => frames.delete(id));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useLiveEvents', () => {
  it('opens exactly one EventSource for the app, on the events stream', () => {
    const { rerender } = mount();
    rerender();
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(source.url).toBe('/api/v1/events');
  });

  it('closes the EventSource on unmount', () => {
    const { unmount } = mount();
    unmount();
    expect(source.closed).toBe(true);
  });

  it('is not ready before the ready event and ready after it', () => {
    const { result } = mount();
    expect(result.current).toBe(false);
    act(() => {
      source.emit('ready');
    });
    expect(result.current).toBe(true);
  });

  it('does not refetch on the first ready, but refetches everything after resync', () => {
    mount();
    const invalidate = vi.spyOn(client, 'invalidateQueries');
    act(() => {
      source.emit('ready');
    });
    expect(invalidate).not.toHaveBeenCalled();
    act(() => {
      source.emit('resync');
    });
    expect(invalidate).toHaveBeenCalledTimes(1);
  });

  describe('On a novel event', () => {
    it('updates [novel, id] and the matching item in [novels]', () => {
      client.setQueryData(['novels'], [novel('NV1'), novel('NV2')]);
      client.setQueryData(['novel', 'NV1'], novel('NV1'));
      mount();
      source.emit('novel', novel('NV1', { downloaded_pages: 5 }));
      runFrame();
      expect(client.getQueryData<Novel>(['novel', 'NV1'])?.downloaded_pages).toBe(5);
      expect(client.getQueryData<Novel[]>(['novels'])?.map((n) => n.downloaded_pages)).toEqual([
        5, 0,
      ]);
    });

    it('inserts an unknown novel into [novels]', () => {
      client.setQueryData(['novels'], [novel('NV1')]);
      mount();
      source.emit('novel', novel('NV9'));
      runFrame();
      expect(client.getQueryData<Novel[]>(['novels'])?.map((n) => n.id)).toEqual(['NV1', 'NV9']);
      expect(client.getQueryData<Novel>(['novel', 'NV9'])?.id).toBe('NV9');
    });

    it('does not keep the sync-only released field in the cache', () => {
      mount();
      source.emit('novel', { ...novel('NV1'), released: [3, 4] });
      runFrame();
      expect(client.getQueryData(['novel', 'NV1'])).not.toHaveProperty('released');
    });

    it('does not create [novels] when the list was never loaded', () => {
      mount();
      source.emit('novel', novel('NV1'));
      runFrame();
      expect(client.getQueryData(['novels'])).toBeUndefined();
    });
  });

  describe('On a novel event with deleted', () => {
    it('removes it from [novels] and marks [novel, id] as deleted', () => {
      client.setQueryData(['novels'], [novel('NV1'), novel('NV2')]);
      client.setQueryData(['novel', 'NV1'], novel('NV1'));
      mount();
      source.emit('novel', { id: 'NV1', deleted: true });
      runFrame();
      expect(client.getQueryData<Novel[]>(['novels'])?.map((n) => n.id)).toEqual(['NV2']);
      const marked = client.getQueryData(['novel', 'NV1']);
      expect(marked).toEqual({ id: 'NV1', deleted: true });
      expect(isDeletedNovel(marked)).toBe(true);
    });
  });

  describe('On a chapter event', () => {
    beforeEach(() => {
      client.setQueryData(['chapters', 'NV1'], [chapter('CH1', 1), chapter('CH3', 3)]);
    });

    it('replaces the chapter in [chapters, novel_id]', () => {
      mount();
      source.emit('chapter', { ...chapter('CH3', 3, { state: 'downloaded' }), novel_id: 'NV1' });
      runFrame();
      const list = client.getQueryData<Chapter[]>(['chapters', 'NV1']);
      expect(list?.map((c) => c.state)).toEqual(['not_requested', 'downloaded']);
      expect(list?.[1]).not.toHaveProperty('novel_id');
    });

    it('inserts an unknown chapter at its position', () => {
      mount();
      source.emit('chapter', { ...chapter('CH2', 2), novel_id: 'NV1' });
      runFrame();
      expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.map((c) => c.id)).toEqual([
        'CH1',
        'CH2',
        'CH3',
      ]);
    });

    it('moves a chapter whose position changed', () => {
      mount();
      source.emit('chapter', { ...chapter('CH1', 4), novel_id: 'NV1' });
      runFrame();
      expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.map((c) => c.id)).toEqual([
        'CH3',
        'CH1',
      ]);
    });

    it('removes a chapter that no longer has a position', () => {
      mount();
      source.emit('chapter', { ...chapter('CH1', 1), position: null, novel_id: 'NV1' });
      runFrame();
      expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.map((c) => c.id)).toEqual([
        'CH3',
      ]);
    });

    it('ignores chapters of a novel whose list is not loaded', () => {
      mount();
      source.emit('chapter', { ...chapter('CH1', 1), novel_id: 'NV7' });
      runFrame();
      expect(client.getQueryData(['chapters', 'NV7'])).toBeUndefined();
    });
  });

  describe('On an export event', () => {
    beforeEach(() => {
      client.setQueryData(['exports', 'NV1'], [exportOf('EX1')]);
    });

    it('upserts it into [exports, novel_id]', () => {
      mount();
      source.emit('export', exportOf('EX1', { state: 'ready' }));
      source.emit('export', exportOf('EX2'));
      runFrame();
      expect(
        client.getQueryData<Export[]>(['exports', 'NV1'])?.map((e) => [e.id, e.state]),
      ).toEqual([
        ['EX1', 'ready'],
        ['EX2', 'waiting'],
      ]);
    });

    it('removes it on deleted', () => {
      mount();
      source.emit('export', { id: 'EX1', novel_id: 'NV1', deleted: true });
      runFrame();
      expect(client.getQueryData<Export[]>(['exports', 'NV1'])).toEqual([]);
    });
  });

  describe('With 1000 chapter events arriving at once', () => {
    it('applies them in one cache update per animation frame', () => {
      client.setQueryData(['chapters', 'NV1'], []);
      mount();
      const set = vi.spyOn(client, 'setQueryData');
      for (let position = 1000; position >= 1; position -= 1) {
        source.emit('chapter', { ...chapter(`CH${String(position)}`, position), novel_id: 'NV1' });
      }
      expect(frames.size).toBe(1);
      expect(set).not.toHaveBeenCalled();
      runFrame();
      expect(set).toHaveBeenCalledTimes(1);
      const list = client.getQueryData<Chapter[]>(['chapters', 'NV1']);
      expect(list).toHaveLength(1000);
      expect(list?.[0]?.position).toBe(1);
      expect(list?.[999]?.position).toBe(1000);
    });
  });

  describe('On reconnect', () => {
    it('relies on the browser Last-Event-ID: no client-side cursor storage', () => {
      const setItem = vi.spyOn(Storage.prototype, 'setItem');
      mount();
      source.emit('ready');
      source.emit('novel', novel('NV1'));
      runFrame();
      expect(setItem).not.toHaveBeenCalled();
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });

  describe('With a REST fetch in flight', () => {
    function deferred<T>() {
      let resolve!: (value: T) => void;
      const promise = new Promise<T>((r) => {
        resolve = r;
      });
      return { promise, resolve };
    }

    it('replays events that arrived during the first load on top of the fetched data', async () => {
      mount();
      const page = deferred<Chapter[]>();
      const load = client.query({
        queryKey: ['chapters', 'NV1'],
        queryFn: () => page.promise,
      });
      source.emit('chapter', {
        ...chapter('CH2', 2, { state: 'downloaded' }),
        novel_id: 'NV1',
      });
      runFrame();
      page.resolve([chapter('CH1', 1), chapter('CH2', 2)]);
      await load;
      await waitFor(() => {
        expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.map((c) => c.state)).toEqual([
          'not_requested',
          'downloaded',
        ]);
      });
    });

    it('replays events that arrived during a refetch that returns older data', async () => {
      client.setQueryData(['chapters', 'NV1'], [chapter('CH1', 1)]);
      client.setQueryData(['novel', 'NV1'], novel('NV1'));
      mount();
      const refetch = deferred<Chapter[]>();
      const load = client.query({
        queryKey: ['chapters', 'NV1'],
        queryFn: () => refetch.promise,
        staleTime: 0,
      });
      const novelRefetch = deferred<Novel>();
      const novelLoad = client.query({
        queryKey: ['novel', 'NV1'],
        queryFn: () => novelRefetch.promise,
        staleTime: 0,
      });
      source.emit('chapter', { ...chapter('CH1', 1, { state: 'downloaded' }), novel_id: 'NV1' });
      source.emit('novel', novel('NV1', { downloaded_pages: 7 }));
      runFrame();
      refetch.resolve([chapter('CH1', 1)]);
      novelRefetch.resolve(novel('NV1'));
      await Promise.all([load, novelLoad]);
      await waitFor(() => {
        expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.[0]?.state).toBe('downloaded');
        expect(client.getQueryData<Novel>(['novel', 'NV1'])?.downloaded_pages).toBe(7);
      });
    });

    it('does not replay events after the fetch has settled', async () => {
      mount();
      await client.query({
        queryKey: ['chapters', 'NV1'],
        queryFn: () => [chapter('CH1', 1)],
      });
      source.emit('chapter', { ...chapter('CH1', 1, { state: 'error' }), novel_id: 'NV1' });
      runFrame();
      await client.query({
        queryKey: ['chapters', 'NV1'],
        queryFn: () => [chapter('CH1', 1, { state: 'downloaded' })],
        staleTime: 0,
      });
      expect(client.getQueryData<Chapter[]>(['chapters', 'NV1'])?.[0]?.state).toBe('downloaded');
    });
  });

  describe('When the connection closes for good', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.useRealTimers();
    });

    it('reopens with backoff and treats the next ready as a resync', () => {
      mount();
      act(() => {
        source.emit('ready');
      });
      const invalidate = vi.spyOn(client, 'invalidateQueries');
      act(() => {
        source.fail();
      });
      expect(FakeEventSource.instances).toHaveLength(1);
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(FakeEventSource.instances).toHaveLength(2);
      expect(FakeEventSource.instances[1]?.closed).toBe(false);
      act(() => {
        FakeEventSource.instances[1]?.emit('ready');
      });
      expect(invalidate).toHaveBeenCalledTimes(1);
    });

    it('backs off further on repeated failures', () => {
      mount();
      act(() => {
        source.fail();
        vi.advanceTimersByTime(1000);
        FakeEventSource.instances[1]?.fail();
        vi.advanceTimersByTime(1000);
      });
      expect(FakeEventSource.instances).toHaveLength(2);
      act(() => {
        vi.advanceTimersByTime(1000);
      });
      expect(FakeEventSource.instances).toHaveLength(3);
    });

    it('does not reopen while the browser is still retrying by itself', () => {
      mount();
      source.readyState = FakeEventSource.CONNECTING;
      act(() => {
        source.listeners.get('error')?.forEach((l) => {
          l(new Event('error'));
        });
        vi.advanceTimersByTime(60_000);
      });
      expect(FakeEventSource.instances).toHaveLength(1);
    });

    it('stops reconnecting after unmount', () => {
      const { unmount } = mount();
      act(() => {
        source.fail();
      });
      unmount();
      vi.advanceTimersByTime(60_000);
      expect(FakeEventSource.instances).toHaveLength(1);
    });
  });

  describe('Ordering of inserted entities', () => {
    it('inserts unknown novels and exports newest first (created_at desc)', () => {
      client.setQueryData(
        ['novels'],
        [
          novel('NV2', { created_at: '2026-02-01T00:00:00.000Z' }),
          novel('NV1', { created_at: '2026-01-01T00:00:00.000Z' }),
        ],
      );
      client.setQueryData(
        ['exports', 'NV1'],
        [exportOf('EX1', { created_at: '2026-01-01T00:00:00.000Z' })],
      );
      mount();
      source.emit('novel', novel('NV3', { created_at: '2026-03-01T00:00:00.000Z' }));
      source.emit('export', exportOf('EX2', { created_at: '2026-01-02T00:00:00.000Z' }));
      runFrame();
      expect(client.getQueryData<Novel[]>(['novels'])?.map((n) => n.id)).toEqual([
        'NV3',
        'NV2',
        'NV1',
      ]);
      expect(client.getQueryData<Export[]>(['exports', 'NV1'])?.map((e) => e.id)).toEqual([
        'EX2',
        'EX1',
      ]);
    });
  });

  describe('On a novel deleted event, child caches', () => {
    it('removes [chapters, id] and [exports, id]', () => {
      client.setQueryData(['chapters', 'NV1'], [chapter('CH1', 1)]);
      client.setQueryData(['exports', 'NV1'], [exportOf('EX1')]);
      client.setQueryData(['chapters', 'NV2'], [chapter('CH9', 1)]);
      mount();
      source.emit('novel', { id: 'NV1', deleted: true });
      runFrame();
      expect(client.getQueryData(['chapters', 'NV1'])).toBeUndefined();
      expect(client.getQueryData(['exports', 'NV1'])).toBeUndefined();
      expect(client.getQueryData(['chapters', 'NV2'])).toBeDefined();
    });
  });

  describe('Malformed payloads', () => {
    it('warns and keeps going', () => {
      const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
      client.setQueryData(['novels'], []);
      mount();
      source.emitRaw('novel', '{not json');
      source.emit('novel', novel('NV1'));
      runFrame();
      expect(warn).toHaveBeenCalledTimes(1);
      expect(client.getQueryData<Novel[]>(['novels'])).toHaveLength(1);
    });

    it('does not swallow errors thrown while handling a valid payload', () => {
      mount();
      // a chapter payload without novel_id is valid JSON but breaks the handler's assumptions
      expect(() => {
        source.emit('export', null);
      }).toThrow();
    });
  });

  describe('Lifecycle', () => {
    it('survives the StrictMode double mount with one live source', () => {
      const { result } = renderHook(
        () => {
          useLiveEvents(client);
          return useLiveReady();
        },
        {
          wrapper: ({ children }: { children: ReactNode }) =>
            createElement(StrictMode, null, children),
        },
      );
      const live = FakeEventSource.instances.filter((i) => !i.closed);
      expect(live).toHaveLength(1);
      act(() => {
        live[0]?.emit('ready');
      });
      expect(result.current).toBe(true);
    });

    it('invalidates everything when ready arrives a second time', () => {
      mount();
      const invalidate = vi.spyOn(client, 'invalidateQueries');
      act(() => {
        source.emit('ready');
        source.emit('ready');
      });
      expect(invalidate).toHaveBeenCalledTimes(1);
    });

    it('drops a pending frame on unmount', () => {
      client.setQueryData(['novels'], []);
      const { unmount } = mount();
      source.emit('novel', novel('NV1'));
      expect(frames.size).toBe(1);
      unmount();
      expect(frames.size).toBe(0);
      runFrame();
      expect(client.getQueryData<Novel[]>(['novels'])).toEqual([]);
    });

    it('resets ready on unmount', () => {
      const { result, unmount } = mount();
      act(() => {
        source.emit('ready');
      });
      expect(result.current).toBe(true);
      unmount();
      expect(renderHook(() => useLiveReady()).result.current).toBe(false);
    });
  });
});
