import type { QueryClient } from '@tanstack/react-query';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Chapter, Export } from '../api/client';
import { makeChapter } from '../test/fixtures';
import { renderWithProviders, testClient } from '../test/render';
import { SelectionBar } from './SelectionBar';

const NOVEL = 'NV000000000001';

function setWide(wide: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: wide,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const makeExport = (overrides: Partial<Export> = {}): Export => ({
  id: 'EX000000000001',
  novel_id: NOVEL,
  filename: 'cartographer-601-630.epub',
  chapter_from: 601,
  chapter_to: 630,
  skip_missing: false,
  format: 'epub',
  mime: 'application/epub+zip',
  state: 'waiting',
  progress: { downloaded: 0, total: 30 },
  blocked: { errors: [], not_queued: [] },
  error: null,
  waited: false,
  size_bytes: null,
  url: null,
  created_at: '2026-10-19T10:00:00.000Z',
  ready_at: null,
  ...overrides,
});

interface Options {
  wide?: boolean;
  exports?: Export[];
  response?: Response | Promise<Response>;
}

function mount(selected: Chapter[], { wide = true, exports = [], response }: Options = {}) {
  setWide(wide);
  const fetchMock = vi.fn(() =>
    Promise.resolve(
      response ?? Response.json({ updated: selected.map((c) => c.position), skipped: [] }),
    ),
  );
  vi.stubGlobal('fetch', fetchMock);
  const client: QueryClient = testClient();
  client.setQueryData(['exports', NOVEL], exports);
  const onDone = vi.fn();
  const onExport = vi.fn();
  renderWithProviders(
    <SelectionBar novelId={NOVEL} selected={selected} onDone={onDone} onExport={onExport} />,
    { client },
  );
  return { fetchMock, onDone, onExport };
}

const sent = (fetchMock: ReturnType<typeof vi.fn>) => {
  const call = fetchMock.mock.calls[0] as [string, RequestInit];
  return {
    url: call[0],
    method: call[1].method,
    body: JSON.parse(call[1].body as string) as unknown,
  };
};

const queued = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, i) => makeChapter(from + i, { state: 'pending' }));

describe('SelectionBar', () => {
  describe('the summary', () => {
    it('shows count, first–last position and the states', () => {
      mount([...queued(607, 613), makeChapter(614, { state: 'error' })]);
      expect(screen.getByText('8 selected')).toBeInTheDocument();
      expect(screen.getByText(/607–614 · 7 queued, 1 error/)).toBeInTheDocument();
    });

    it('shows just "0 selected" when nothing is selected', () => {
      mount([]);
      expect(screen.getByText('0 selected')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
    });
  });

  describe('on desktop', () => {
    it('has Download, Cancel queued, Retry errors and Export EPUB…', () => {
      mount(queued(1, 2));
      for (const name of ['Download', 'Cancel queued', 'Retry errors', 'Export EPUB…']) {
        expect(screen.getByRole('button', { name })).toBeInTheDocument();
      }
      expect(screen.queryByRole('button', { name: 'More…' })).toBeNull();
    });

    it('Download requests the pending state for the selected positions', async () => {
      const { fetchMock } = mount([
        makeChapter(3, { state: 'not_requested' }),
        makeChapter(5, { state: 'not_requested' }),
      ]);
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(sent(fetchMock)).toEqual({
        url: `/api/v1/novels/${NOVEL}/chapters`,
        method: 'PATCH',
        body: { state: 'pending', chapters: [3, 5] },
      });
    });

    it('Download never re-downloads: a mixed selection sends only the not_requested chapters', async () => {
      const mixed = [
        makeChapter(1),
        makeChapter(2, { state: 'not_requested' }),
        makeChapter(3, { state: 'error' }),
        makeChapter(4, { state: 'not_requested' }),
        makeChapter(5, { state: 'pending' }),
      ];
      const { fetchMock } = mount(mixed, {
        response: Response.json({
          updated: [2],
          skipped: [{ position: 4, state: 'downloading' }],
        }),
      });
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(sent(fetchMock).body).toEqual({ state: 'pending', chapters: [2, 4] });
      // 3 selected chapters were not sent, plus 1 skipped by the server.
      expect(await screen.findByText('Queued 1 · skipped 4')).toBeInTheDocument();
    });

    it('Retry errors counts the chapters it left out as skipped', async () => {
      mount([makeChapter(1), makeChapter(2, { state: 'error' })], {
        response: Response.json({ updated: [2], skipped: [] }),
      });
      await userEvent.click(screen.getByRole('button', { name: 'Retry errors' }));
      expect(await screen.findByText('Queued 1 · skipped 1')).toBeInTheDocument();
    });

    it('disables Download when nothing selected is waiting to be requested', () => {
      mount([makeChapter(1), makeChapter(2, { state: 'error' })]);
      expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();
    });

    it('Cancel queued requests not_requested when no waiting export needs the chapters', async () => {
      const { fetchMock } = mount(queued(1, 2), {
        exports: [makeExport({ state: 'ready' }), makeExport({ skip_missing: true })],
      });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(sent(fetchMock).body).toEqual({ state: 'not_requested', chapters: [1, 2] });
    });

    it('Retry errors requests pending for the errored chapters only', async () => {
      const { fetchMock } = mount([
        makeChapter(1),
        makeChapter(2, { state: 'error' }),
        makeChapter(3, { state: 'error' }),
      ]);
      await userEvent.click(screen.getByRole('button', { name: 'Retry errors' }));
      expect(sent(fetchMock).body).toEqual({ state: 'pending', chapters: [2, 3] });
    });

    it('disables Retry errors without errors and Cancel queued without queued chapters', () => {
      mount([makeChapter(1)]);
      expect(screen.getByRole('button', { name: 'Retry errors' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Cancel queued' })).toBeDisabled();
    });

    it('Export EPUB… passes the first and last position to onExport', async () => {
      const { onExport, fetchMock } = mount(
        [makeChapter(4), makeChapter(9), makeChapter(7)].sort((a, b) => a.position - b.position),
      );
      await userEvent.click(screen.getByRole('button', { name: 'Export EPUB…' }));
      expect(onExport).toHaveBeenCalledExactlyOnceWith(4, 9);
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('the confirmation for Cancel queued', () => {
    const chapters = queued(605, 607);

    it('names a waiting export that needs the chapters and sends nothing yet', async () => {
      const { fetchMock } = mount(chapters, { exports: [makeExport()] });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveTextContent('cartographer-601-630.epub');
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it('says the named export will fail', async () => {
      mount(chapters, { exports: [makeExport()] });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      expect(screen.getByRole('dialog')).toHaveTextContent(/will fail the export/);
    });

    it('Keep queued closes it without a request', async () => {
      const { fetchMock, onDone } = mount(chapters, { exports: [makeExport()] });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      await userEvent.click(screen.getByRole('button', { name: 'Keep queued' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(fetchMock).not.toHaveBeenCalled();
      expect(onDone).not.toHaveBeenCalled();
    });

    it('confirming sends the request', async () => {
      const { fetchMock } = mount(chapters, { exports: [makeExport()] });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      await userEvent.click(
        within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel queued' }),
      );
      await waitFor(() => {
        expect(sent(fetchMock).body).toEqual({ state: 'not_requested', chapters: [605, 606, 607] });
      });
    });

    it('is not asked when the export range does not cover a queued chapter', async () => {
      const { fetchMock } = mount(chapters, {
        exports: [makeExport({ chapter_from: 1, chapter_to: 50 })],
      });
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(fetchMock).toHaveBeenCalledOnce();
    });
  });

  describe('after a bulk action', () => {
    it('shows "Queued N · skipped M" and leaves select mode', async () => {
      const fresh = [1, 2, 3].map((n) => makeChapter(n, { state: 'not_requested' }));
      const { onDone } = mount(fresh, {
        response: Response.json({
          updated: [1, 2],
          skipped: [{ position: 3, state: 'downloading' }],
        }),
      });
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(await screen.findByText('Queued 2 · skipped 1')).toBeInTheDocument();
      expect(onDone).toHaveBeenCalledOnce();
    });

    it('on failure shows the error and stays in select mode', async () => {
      const fresh = [1, 2, 3].map((n) => makeChapter(n, { state: 'not_requested' }));
      const { onDone } = mount(fresh, {
        response: Response.json(
          { error: { code: 'validation_error', message: 'Bad selector' } },
          { status: 422 },
        ),
      });
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(await screen.findByRole('alert')).toHaveTextContent('Bad selector');
      expect(onDone).not.toHaveBeenCalled();
      expect(screen.queryByText(/Queued/)).toBeNull();
    });
  });

  describe('on a phone', () => {
    it('has Download, Retry and More… only', () => {
      mount(queued(1, 2), { wide: false });
      expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'More…' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Cancel queued' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Export EPUB…' })).toBeNull();
    });

    it('More… opens a bottom sheet with Cancel queued and Export EPUB…', async () => {
      const { onExport } = mount(queued(10, 12), { wide: false });
      await userEvent.click(screen.getByRole('button', { name: 'More…' }));
      const sheet = screen.getByRole('dialog', { name: 'More actions' });
      expect(within(sheet).getByRole('button', { name: 'Cancel queued' })).toBeInTheDocument();
      await userEvent.click(within(sheet).getByRole('button', { name: 'Export EPUB…' }));
      expect(onExport).toHaveBeenCalledExactlyOnceWith(10, 12);
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('Cancel queued from the sheet sends the request', async () => {
      const { fetchMock } = mount(queued(10, 12), { wide: false });
      await userEvent.click(screen.getByRole('button', { name: 'More…' }));
      await userEvent.click(screen.getByRole('button', { name: 'Cancel queued' }));
      expect(sent(fetchMock).body).toEqual({ state: 'not_requested', chapters: [10, 11, 12] });
    });
  });
});
