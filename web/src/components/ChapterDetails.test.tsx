import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { makeChapter, makeChapters } from '../test/fixtures';
import { renderWithProviders, testClient } from '../test/render';
import { useChapters } from '../hooks/useChapters';
import { ChapterDetails } from './ChapterDetails';

const NOVEL = 'NV000000000001';

function stubMedia(wide: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: query.includes('min-width') ? wide : false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

beforeEach(() => {
  stubMedia(true);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function show(
  chapter: Chapter,
  {
    chapters = makeChapters(5),
    onStep = vi.fn(),
    onClose = vi.fn(),
  }: { chapters?: Chapter[]; onStep?: (id: string) => void; onClose?: () => void } = {},
) {
  const all = chapters.map((c) => (c.id === chapter.id ? chapter : c));
  const utils = renderWithProviders(
    <ChapterDetails
      novelId={NOVEL}
      chapterId={chapter.id}
      chapters={all}
      rows={all}
      onStep={onStep}
      onClose={onClose}
    />,
  );
  return { ...utils, onStep, onClose, all };
}

describe('ChapterDetails', () => {
  describe('layout', () => {
    it('is a modal dialog at 720 px and wider', () => {
      show(makeChapter(2));
      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveClass('m-auto');
      expect(dialog).not.toHaveClass('mt-auto');
      expect(screen.queryByTestId('sheet-handle')).toBeNull();
    });

    it('is a bottom sheet below 720 px', () => {
      stubMedia(false);
      show(makeChapter(2));
      expect(screen.getByRole('dialog')).toHaveClass('mt-auto');
      expect(screen.getByTestId('sheet-handle')).toBeInTheDocument();
    });
  });

  describe('heading and navigation', () => {
    it('shows "Chapter N of M" and the chapter title', () => {
      show(makeChapter(2, { title: 'The Long Way' }));
      expect(screen.getByRole('heading', { name: 'Chapter 2 of 5' })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'The Long Way' })).toBeInTheDocument();
    });

    it('falls back to "Chapter N" for a chapter without a title', () => {
      show(makeChapter(2, { title: null }));
      expect(screen.getByRole('heading', { name: 'Chapter 2' })).toBeInTheDocument();
    });

    it('Next and Previous step to the neighbouring rows', async () => {
      const { onStep, all } = show(makeChapter(3));
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(onStep).toHaveBeenLastCalledWith(all[3]?.id);
      await userEvent.click(screen.getByRole('button', { name: 'Previous chapter' }));
      expect(onStep).toHaveBeenLastCalledWith(all[1]?.id);
    });

    it('steps through the given rows, not the whole novel', async () => {
      const chapters = makeChapters(5);
      const onStep = vi.fn();
      const rows = [chapters[0], chapters[3]].filter((c): c is Chapter => c !== undefined);
      renderWithProviders(
        <ChapterDetails
          novelId={NOVEL}
          chapterId={rows[0]?.id ?? ''}
          chapters={chapters}
          rows={rows}
          onStep={onStep}
          onClose={vi.fn()}
        />,
      );
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(onStep).toHaveBeenCalledWith(chapters[3]?.id);
    });

    it('stops at the ends: Previous on the first and Next on the last do nothing', async () => {
      const first = show(makeChapter(1));
      const prev = screen.getByRole('button', { name: 'Previous chapter' });
      expect(prev).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(prev);
      expect(first.onStep).not.toHaveBeenCalled();
      first.unmount();
      const last = show(makeChapter(5));
      const next = screen.getByRole('button', { name: 'Next chapter' });
      expect(next).toHaveAttribute('aria-disabled', 'true');
      await userEvent.click(next);
      expect(last.onStep).not.toHaveBeenCalled();
    });

    it('steps through every chapter when the open one is not among the rows', async () => {
      const chapters = makeChapters(5);
      const onStep = vi.fn();
      renderWithProviders(
        <ChapterDetails
          novelId={NOVEL}
          chapterId={chapters[2]?.id ?? ''}
          chapters={chapters}
          rows={[chapters[0] as Chapter]}
          onStep={onStep}
          onClose={vi.fn()}
        />,
      );
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(onStep).toHaveBeenCalledWith(chapters[3]?.id);
    });

    it('the Close button and Escape close it', async () => {
      const { onClose } = show(makeChapter(2));
      await userEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(onClose).toHaveBeenCalledTimes(1);
      await userEvent.keyboard('{Escape}');
      expect(onClose).toHaveBeenCalledTimes(2);
    });

    it('swiping down on the sheet handle closes it; a short drag does not', () => {
      stubMedia(false);
      const { onClose } = show(makeChapter(2));
      const handle = screen.getByTestId('sheet-handle');
      fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 });
      fireEvent.pointerUp(handle, { clientY: 130, pointerId: 1 });
      expect(onClose).not.toHaveBeenCalled();
      fireEvent.pointerDown(handle, { clientY: 100, pointerId: 1 });
      fireEvent.pointerUp(handle, { clientY: 220, pointerId: 1 });
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('disappears when the chapter is no longer in the list', () => {
      const chapters = makeChapters(3);
      renderWithProviders(
        <ChapterDetails
          novelId={NOVEL}
          chapterId="CH999999999999"
          chapters={chapters}
          rows={chapters}
          onStep={vi.fn()}
          onClose={vi.fn()}
        />,
      );
      expect(screen.queryByRole('dialog')).toBeNull();
    });
  });

  describe('error box', () => {
    const failed = () =>
      makeChapter(2, {
        state: 'error',
        error: 'TimeoutError: no response after 3 retries',
        error_at: new Date(Date.now() - 8 * 60_000).toISOString(),
      });

    it('shows the failure time and the full message in monospace', () => {
      show(failed());
      const box = screen.getByRole('group', { name: 'Error details' });
      expect(within(box).getByText('failed 8 min ago')).toBeInTheDocument();
      const message = within(box).getByText('TimeoutError: no response after 3 retries');
      expect(message).toHaveClass('font-mono');
    });

    it('is absent in any other state', () => {
      show(makeChapter(2, { state: 'downloaded' }));
      expect(screen.queryByRole('group', { name: 'Error details' })).toBeNull();
    });

    it('omits the time when the failure time is unknown', () => {
      show({ ...failed(), error_at: null });
      expect(screen.queryByText(/^failed /)).toBeNull();
    });

    it('Copy writes the message to the clipboard', async () => {
      const writeText = vi.fn(() => Promise.resolve());
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      show(failed());
      await userEvent.click(screen.getByRole('button', { name: 'Copy error message' }));
      expect(writeText).toHaveBeenCalledWith('TimeoutError: no response after 3 retries');
      expect(await screen.findByText('Copied')).toBeInTheDocument();
    });

    it('Copy selects the text when the clipboard is unavailable', async () => {
      vi.stubGlobal('navigator', { clipboard: undefined });
      show(failed());
      await userEvent.click(screen.getByRole('button', { name: 'Copy error message' }));
      expect(window.getSelection()?.toString()).toBe('TimeoutError: no response after 3 retries');
      expect(await screen.findByText('Text selected, copy it')).toBeInTheDocument();
    });

    it('Copy selects the text when the clipboard rejects', async () => {
      const writeText = vi.fn(() => Promise.reject(new Error('denied')));
      vi.stubGlobal('navigator', { clipboard: { writeText } });
      show(failed());
      await userEvent.click(screen.getByRole('button', { name: 'Copy error message' }));
      await waitFor(() => {
        expect(window.getSelection()?.toString()).toBe('TimeoutError: no response after 3 retries');
      });
    });
  });

  describe('fields', () => {
    const value = (label: string) => {
      const term = screen.getByText(label, { selector: 'dt' });
      return term.nextElementSibling as HTMLElement;
    };

    it('shows status, a source link and the added date', () => {
      const chapter = makeChapter(2, { released_at: '2026-10-16T12:00:00.000Z' });
      show(chapter);
      expect(value('Status')).toHaveTextContent('Downloaded');
      const link = within(value('Source')).getByRole('link');
      expect(link).toHaveAttribute('href', chapter.url);
      expect(value('Added').textContent).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    });

    it('shows a dash for added when there is no release date', () => {
      show(makeChapter(2, { released_at: null }));
      expect(value('Added')).toHaveTextContent('—');
    });

    it('shows Queued only for a pending chapter', () => {
      const pending = makeChapter(2, { state: 'pending', queued_at: '2026-10-16T09:12:00.000Z' });
      const first = show(pending);
      expect(screen.getByText('Queued', { selector: 'dt' })).toBeInTheDocument();
      first.unmount();
      show(makeChapter(2, { state: 'downloaded' }));
      expect(screen.queryByText('Queued', { selector: 'dt' })).toBeNull();
    });

    it('shows the last change', () => {
      show(makeChapter(2, { updated_at: '2026-10-01T10:00:00.000Z' }));
      expect(value('Last change').textContent).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
    });

    it.each([
      [{ state: 'downloaded', has_images: true }, 'yes'],
      [{ state: 'downloaded', has_images: false }, 'no'],
      [{ state: 'not_requested', has_images: false }, 'not downloaded yet'],
      [{ state: 'error', has_images: false }, 'not downloaded yet'],
    ] as const)('images for %j is "%s"', (overrides, text) => {
      show(makeChapter(2, overrides));
      expect(value('Images')).toHaveTextContent(text);
    });

    it('has an Open on site link to the chapter URL', () => {
      const chapter = makeChapter(2);
      show(chapter);
      const link = screen.getByRole('link', { name: /Open on site/ });
      expect(link).toHaveAttribute('href', chapter.url);
      expect(link).toHaveAttribute('target', '_blank');
      expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));
    });
  });

  describe('primary action', () => {
    function stubPatch(result: unknown = { updated: [2], skipped: [] }, ok = true) {
      const fetchMock = vi.fn(() =>
        Promise.resolve(
          ok
            ? Response.json(result)
            : Response.json(
                { error: { code: 'boom', message: 'Server said no' } },
                { status: 500 },
              ),
        ),
      );
      vi.stubGlobal('fetch', fetchMock);
      return fetchMock;
    }

    it.each([
      ['not_requested', 'Download', 'pending'],
      ['pending', 'Cancel', 'not_requested'],
      ['error', 'Retry download', 'pending'],
      ['downloaded', 'Download again', 'pending'],
    ] as const)(
      '%s offers "%s" and sends PATCH state %s with a one-item list',
      async (state, label, next) => {
        const fetchMock = stubPatch();
        show(makeChapter(2, { state }));
        await userEvent.click(screen.getByRole('button', { name: label }));
        await waitFor(() => {
          expect(fetchMock).toHaveBeenCalledTimes(1);
        });
        const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
        expect(url).toBe(`/api/v1/novels/${NOVEL}/chapters`);
        expect(init.method).toBe('PATCH');
        expect(JSON.parse(init.body as string)).toEqual({ state: next, chapters: [2] });
      },
    );

    it('has no primary action while downloading', () => {
      show(makeChapter(2, { state: 'downloading' }));
      for (const name of ['Download', 'Cancel', 'Retry download', 'Download again']) {
        expect(screen.queryByRole('button', { name })).toBeNull();
      }
    });

    it('Download again is secondary style, Download is primary', () => {
      const again = show(makeChapter(2, { state: 'downloaded' }));
      expect(screen.getByRole('button', { name: 'Download again' })).not.toHaveClass('bg-accent');
      again.unmount();
      show(makeChapter(2, { state: 'not_requested' }));
      expect(screen.getByRole('button', { name: 'Download' })).toHaveClass('bg-accent');
    });

    it('shows the API error and stays open when the request fails', async () => {
      stubPatch(undefined, false);
      const { onClose } = show(makeChapter(2, { state: 'error', error: 'x' }));
      await userEvent.click(screen.getByRole('button', { name: 'Retry download' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Server said no');
      expect(onClose).not.toHaveBeenCalled();
    });

    it('an error on one chapter does not show after stepping to the next', async () => {
      stubPatch(undefined, false);
      const chapters = makeChapters(3);
      function Stepper() {
        const [id, setId] = useState(chapters[0]?.id ?? '');
        return (
          <ChapterDetails
            novelId={NOVEL}
            chapterId={id}
            chapters={chapters.map((c) => ({ ...c, state: 'not_requested' as const }))}
            rows={chapters}
            onStep={setId}
            onClose={vi.fn()}
          />
        );
      }
      renderWithProviders(<Stepper />);
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Server said no');
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(screen.getByRole('heading', { name: 'Chapter 2 of 3' })).toBeInTheDocument();
      expect(screen.queryByRole('alert')).toBeNull();
      expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled();
    });

    it('says so when the server skipped the chapter', async () => {
      stubPatch({ updated: [], skipped: [{ position: 2, state: 'downloading' }] });
      show(makeChapter(2, { state: 'not_requested' }));
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(await screen.findByRole('alert')).toHaveTextContent(/changed/);
    });
  });

  describe('live updates', () => {
    it('shows the new state when the chapters change', async () => {
      const chapters = makeChapters(3);
      const client = testClient();
      client.setQueryData(['chapters', NOVEL], chapters);
      function Live() {
        const { data = [] } = useChapters(NOVEL);
        return (
          <ChapterDetails
            novelId={NOVEL}
            chapterId={chapters[1]?.id ?? ''}
            chapters={data}
            rows={data}
            onStep={vi.fn()}
            onClose={vi.fn()}
          />
        );
      }
      renderWithProviders(<Live />, { client });
      expect(screen.getByRole('button', { name: 'Download again' })).toBeInTheDocument();
      act(() => {
        client.setQueryData(
          ['chapters', NOVEL],
          chapters.map((c) => (c.position === 2 ? { ...c, state: 'pending' as const } : c)),
        );
      });
      expect(await screen.findByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    });
  });
});
