import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { setLiveReady } from './useLiveEvents';
import { useChapters } from './useChapters';

const TOTAL = 2500;

function stubFetch() {
  const fetchMock = vi.fn((input: string) => {
    const params = new URL(input, 'http://x').searchParams;
    const offset = Number(params.get('offset'));
    const limit = Number(params.get('limit'));
    const items = Array.from({ length: Math.min(limit, TOTAL - offset) }, (_, i) => ({
      id: `CH${String(offset + i + 1)}`,
      position: offset + i + 1,
    }));
    return Promise.resolve(Response.json({ items, total: TOTAL }));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function wrapperFor(client: QueryClient) {
  return ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
}

afterEach(() => {
  act(() => {
    setLiveReady(false);
  });
  vi.unstubAllGlobals();
});

describe('useChapters', () => {
  it('for a novel with 2500 chapters fetches pages until total and exposes all in order', async () => {
    const fetchMock = stubFetch();
    act(() => {
      setLiveReady(true);
    });
    const client = new QueryClient();
    const { result } = renderHook(() => useChapters('NV1'), { wrapper: wrapperFor(client) });
    await waitFor(() => {
      expect(result.current.data).toHaveLength(TOTAL);
    });
    const positions = (result.current.data as Chapter[]).map((c) => c.position);
    expect(positions).toEqual(Array.from({ length: TOTAL }, (_, i) => i + 1));
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(client.getQueryData(['chapters', 'NV1'])).toHaveLength(TOTAL);
  });

  it('does not fetch before the live stream is ready', () => {
    const fetchMock = stubFetch();
    const client = new QueryClient();
    renderHook(() => useChapters('NV1'), { wrapper: wrapperFor(client) });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
