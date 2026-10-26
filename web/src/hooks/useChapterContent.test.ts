import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import { createElement, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { FakeEventSource } from '../test/FakeEventSource';
import { makeChapter } from '../test/fixtures';
import { chapterContentQuery, useChapterContent } from './useChapterContent';
import { useLiveEvents } from './useLiveEvents';

const NOVEL = 'NV000000000001';

function stubContent() {
  const fetchMock = vi.fn((url: string) =>
    Promise.resolve(Response.json({ markdown: `Text of ${url}` })),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function mount(chapter: Chapter | undefined) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(
    ({ current }: { current: Chapter | undefined }) => {
      useLiveEvents(client);
      return useChapterContent(NOVEL, current);
    },
    { wrapper, initialProps: { current: chapter } },
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

describe('useChapterContent', () => {
  describe('chapter downloaded', () => {
    it('fetches and returns the markdown', async () => {
      const fetchMock = stubContent();
      const chapter = makeChapter(3);
      const { result } = mount(chapter);
      ready();
      await waitFor(() => {
        expect(result.current.data?.markdown).toContain(`/chapters/${chapter.id}/content`);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
  });

  describe('chapter not downloaded', () => {
    it.each(['not_requested', 'pending', 'downloading', 'error'] as const)(
      'does not fetch while it is %s',
      (state) => {
        const fetchMock = stubContent();
        mount(makeChapter(3, { state }));
        ready();
        expect(fetchMock).not.toHaveBeenCalled();
      },
    );

    it('does not fetch without a chapter', () => {
      const fetchMock = stubContent();
      mount(undefined);
      ready();
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('chapter re-downloaded (updated_at changes)', () => {
    it('fetches again under the new key', async () => {
      const fetchMock = stubContent();
      const { result, rerender } = mount(makeChapter(3));
      ready();
      await waitFor(() => {
        expect(result.current.data).toBeDefined();
      });
      rerender({ current: makeChapter(3, { updated_at: '2026-10-02T10:00:00.000Z' }) });
      await waitFor(() => {
        expect(fetchMock).toHaveBeenCalledTimes(2);
      });
    });
  });

  describe('live stream not ready', () => {
    it('does not fetch', () => {
      const fetchMock = stubContent();
      mount(makeChapter(3));
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('the cached text of a chapter', () => {
    it('is never stale: asking again for the same chapter does not fetch it again', async () => {
      const fetchMock = stubContent();
      const client = new QueryClient();
      const chapter = makeChapter(3);
      await client.query(chapterContentQuery(NOVEL, chapter));
      await client.query(chapterContentQuery(NOVEL, chapter));
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });

    it('is fetched again once the chapter was downloaded again (a new key)', async () => {
      const fetchMock = stubContent();
      const client = new QueryClient();
      await client.query(chapterContentQuery(NOVEL, makeChapter(3)));
      await client.query(
        chapterContentQuery(NOVEL, makeChapter(3, { updated_at: '2026-10-02T10:00:00.000Z' })),
      );
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
  });
});
