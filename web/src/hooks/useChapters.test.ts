import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { FakeEventSource } from '../test/FakeEventSource';
import { useChapters } from './useChapters';
import { useLiveEvents } from './useLiveEvents';

type PageFn = (
  offset: number,
  limit: number,
) => { items: { id: string; position: number }[]; total: number };

function chaptersOf(from: number, to: number) {
  return Array.from({ length: Math.max(0, to - from + 1) }, (_, i) => ({
    id: `CH${String(from + i)}`,
    position: from + i,
  }));
}

function stubFetch(pageFn: PageFn) {
  const fetchMock = vi.fn((input: string) => {
    const params = new URL(input, 'http://x').searchParams;
    return Promise.resolve(
      Response.json(pageFn(Number(params.get('offset')), Number(params.get('limit')))),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const evenPages =
  (total: number): PageFn =>
  (offset, limit) => ({ items: chaptersOf(offset + 1, Math.min(offset + limit, total)), total });

function mount(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(
    () => {
      useLiveEvents(client);
      return useChapters('NV1');
    },
    { wrapper },
  );
}

function ready() {
  act(() => {
    FakeEventSource.instances[0]?.emit('ready');
  });
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useChapters', () => {
  it('for a novel with 2500 chapters fetches pages until total and exposes all in order', async () => {
    const fetchMock = stubFetch(evenPages(2500));
    const client = new QueryClient();
    const { result } = mount(client);
    ready();
    await waitFor(() => {
      expect(result.current.data).toHaveLength(2500);
    });
    const positions = (result.current.data as Chapter[]).map((c) => c.position);
    expect(positions).toEqual(Array.from({ length: 2500 }, (_, i) => i + 1));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(client.getQueryData(['chapters', 'NV1'])).toHaveLength(2500);
  });

  it('does not fetch before the live stream is ready', () => {
    const fetchMock = stubFetch(evenPages(10));
    mount(new QueryClient());
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('dedupes chapters repeated across pages', async () => {
    // a sync inserted a chapter between page requests, so offset 1000 repeats CH1000
    stubFetch((offset) =>
      offset === 0
        ? { items: chaptersOf(1, 1000), total: 1500 }
        : { items: chaptersOf(1000, 1500), total: 1500 },
    );
    const { result } = mount(new QueryClient());
    ready();
    await waitFor(() => {
      expect(result.current.data).toHaveLength(1500);
    });
  });

  it('restarts the load when total changed between pages', async () => {
    let calls = 0;
    const fetchMock = stubFetch((offset, limit) => {
      calls += 1;
      const total = calls === 1 ? 1500 : 1501;
      return { items: chaptersOf(offset + 1, Math.min(offset + limit, total)), total };
    });
    const { result } = mount(new QueryClient());
    ready();
    await waitFor(() => {
      expect(result.current.data).toHaveLength(1501);
    });
    expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(4);
  });
});
