import { QueryClient } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter, Export, Novel } from '../api/client';
import { isDeletedNovel } from '../api/events';
import { useLiveEvents, useLiveReady } from './useLiveEvents';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, ((event: MessageEvent) => void)[]>();
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(name: string, listener: (event: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), listener]);
  }
  close() {
    this.closed = true;
  }
  emit(name: string, data: unknown = {}) {
    for (const listener of this.listeners.get(name) ?? []) {
      listener(new MessageEvent(name, { data: JSON.stringify(data) }));
    }
  }
}

let frames: FrameRequestCallback[] = [];
function runFrame() {
  const pending = frames;
  frames = [];
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
  frames = [];
  client = new QueryClient();
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
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
      expect(frames).toHaveLength(1);
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
});
