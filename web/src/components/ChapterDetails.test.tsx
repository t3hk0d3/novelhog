import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter, Export } from '../api/client';
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
    live = false,
  }: {
    chapters?: Chapter[];
    onStep?: (id: string) => void;
    onClose?: () => void;
    live?: boolean;
  } = {},
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
    { live },
  );
  return { ...utils, onStep, onClose, all };
}

describe('ChapterDetails', () => {
  describe('Preview button', () => {
    function stubContent() {
      const fetchMock = vi.fn((url: string) => {
        if (url.endsWith('/exports')) return Promise.resolve(Response.json([]));
        return Promise.resolve(Response.json({ markdown: 'Story text' }));
      });
      vi.stubGlobal('fetch', fetchMock);
      return fetchMock;
    }
    const contentCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
      fetchMock.mock.calls.filter(([url]) => (url as string).includes('/content'));

    it('is offered for a downloaded chapter, which shows no inline content', () => {
      stubContent();
      show(makeChapter(2), { live: true });
      expect(screen.getByRole('button', { name: 'Preview' })).toBeEnabled();
      expect(screen.queryByRole('region', { name: 'Chapter content' })).toBeNull();
    });

    it('does not request the content until it is opened', () => {
      const fetchMock = stubContent();
      show(makeChapter(2), { live: true });
      expect(contentCalls(fetchMock)).toHaveLength(0);
    });

    it('opens the preview dialog over the details, and Close returns to the details', async () => {
      stubContent();
      show(makeChapter(2, { title: 'The Gate' }), { live: true });
      await userEvent.click(screen.getByRole('button', { name: 'Preview' }));
      const preview = screen.getByRole('dialog', { name: 'The Gate' });
      expect(await within(preview).findByText('Story text')).toBeInTheDocument();
      expect(screen.getByRole('dialog', { name: 'Chapter 2 of 5' })).toBeInTheDocument();
      await userEvent.click(within(preview).getByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog', { name: 'The Gate' })).toBeNull();
      expect(screen.getByRole('dialog', { name: 'Chapter 2 of 5' })).toBeInTheDocument();
    });

    it('Back in the preview returns to the details', async () => {
      stubContent();
      show(makeChapter(2, { title: 'The Gate' }), { live: true });
      await userEvent.click(screen.getByRole('button', { name: 'Preview' }));
      await userEvent.click(
        within(screen.getByRole('dialog', { name: 'The Gate' })).getByRole('button', {
          name: 'Back',
        }),
      );
      expect(screen.queryByRole('dialog', { name: 'The Gate' })).toBeNull();
      expect(screen.getByRole('dialog', { name: 'Chapter 2 of 5' })).toBeInTheDocument();
    });

    it.each(['not_requested', 'pending', 'downloading', 'error'] as const)(
      'is not offered while the chapter is %s',
      (state) => {
        stubContent();
        show(makeChapter(2, { state }), { live: true });
        expect(screen.queryByRole('button', { name: 'Preview' })).toBeNull();
        expect(screen.queryByRole('link', { name: 'Read' })).toBeNull();
      },
    );

    it('is an inline button among the fields, not in the bottom toolbar', () => {
      stubContent();
      show(makeChapter(2), { live: true });
      expect(screen.getByRole('button', { name: 'Preview' }).closest('dl')).not.toBeNull();
    });

    it('puts Read last in the toolbar, after the state-dependent action', () => {
      stubContent();
      show(makeChapter(2), { live: true });
      const toolbar = screen.getByRole('link', { name: 'Open on site' }).parentElement;
      expect(toolbar?.lastElementChild).toHaveTextContent('Read');
      expect(screen.getByRole('button', { name: 'Download again' }).nextElementSibling).toBe(
        screen.getByRole('link', { name: 'Read' }),
      );
    });

    it('has a Read link to the reader page in the toolbar', () => {
      stubContent();
      const chapter = makeChapter(2);
      show(chapter, { live: true });
      expect(screen.getByRole('link', { name: 'Read' })).toHaveAttribute(
        'href',
        `/novels/${NOVEL}/chapters/${chapter.id}`,
      );
    });
  });

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

    describe('Cancel on a pending chapter', () => {
      const waiting = {
        id: 'EX000000000001',
        filename: 'tale-1-5.epub',
        chapter_from: 1,
        chapter_to: 5,
        skip_missing: false,
        state: 'waiting',
      } as unknown as Export;

      function showWith(exports: Export[]) {
        const fetchMock = stubPatch();
        const client = testClient();
        client.setQueryData(['exports', NOVEL], exports);
        const chapter = makeChapter(2, { state: 'pending' });
        const all = makeChapters(5).map((c) => (c.id === chapter.id ? chapter : c));
        renderWithProviders(
          <ChapterDetails
            novelId={NOVEL}
            chapterId={chapter.id}
            chapters={all}
            rows={all}
            onStep={vi.fn()}
            onClose={vi.fn()}
          />,
          { client },
        );
        return fetchMock;
      }

      it('asks first, naming the export and saying it will fail', async () => {
        const fetchMock = showWith([waiting]);
        await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        const dialogs = screen.getAllByRole('dialog');
        const confirm = dialogs[dialogs.length - 1] as HTMLElement;
        expect(confirm).toHaveTextContent('tale-1-5.epub');
        expect(confirm).toHaveTextContent(/will fail the export/);
        expect(fetchMock).not.toHaveBeenCalled();
      });

      it('confirming sends the PATCH', async () => {
        const fetchMock = showWith([waiting]);
        await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
        await waitFor(() => {
          expect(fetchMock).toHaveBeenCalledTimes(1);
        });
        const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1];
        expect(JSON.parse(init.body as string)).toEqual({ state: 'not_requested', chapters: [2] });
      });

      it('does not carry a confirmation over to another chapter', async () => {
        const fetchMock = stubPatch();
        const client = testClient();
        client.setQueryData(['exports', NOVEL], [waiting]);
        let setSecond: (state: Chapter['state']) => void = () => undefined;
        const Host = () => {
          const [id, setId] = useState(makeChapter(2).id);
          const [second, set] = useState<Chapter['state']>('pending');
          setSecond = set;
          const all = [
            makeChapter(1),
            makeChapter(2, { state: second }),
            makeChapter(3, { state: 'pending' }),
            makeChapter(4),
            makeChapter(5),
          ];
          return (
            <ChapterDetails
              novelId={NOVEL}
              chapterId={id}
              chapters={all}
              rows={all}
              onStep={setId}
              onClose={vi.fn()}
            />
          );
        };
        renderWithProviders(<Host />, { client });
        await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        expect(screen.getByRole('button', { name: 'Keep queued' })).toBeInTheDocument();
        // A live update starts chapter 2: the need goes away and the confirmation with it.
        act(() => {
          setSecond('downloading');
        });
        expect(screen.queryByRole('button', { name: 'Keep queued' })).toBeNull();
        await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
        expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
        expect(screen.queryByRole('button', { name: 'Keep queued' })).toBeNull();
        expect(fetchMock).not.toHaveBeenCalled();
      });

      it('dismissing sends nothing', async () => {
        const fetchMock = showWith([waiting]);
        await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await userEvent.click(screen.getByRole('button', { name: 'Keep queued' }));
        expect(screen.queryByRole('button', { name: 'Keep queued' })).toBeNull();
        expect(fetchMock).not.toHaveBeenCalled();
      });

      it('sends the PATCH without asking when no waiting export needs the chapter', async () => {
        const fetchMock = showWith([{ ...waiting, skip_missing: true }]);
        await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
        await waitFor(() => {
          expect(fetchMock).toHaveBeenCalledTimes(1);
        });
        expect(screen.queryByRole('button', { name: 'Keep queued' })).toBeNull();
      });
    });

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
