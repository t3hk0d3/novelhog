import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { QueryClient } from '@tanstack/react-query';
import type { Export } from '../api/client';
import { renderWithProviders, testClient } from '../test/render';
import { ExportsPanel } from './ExportsPanel';

const NOVEL = 'NV000000000001';

afterEach(() => {
  vi.unstubAllGlobals();
});

const makeExport = (overrides: Partial<Export> = {}): Export => ({
  id: 'EX000000000001',
  novel_id: NOVEL,
  filename: 'Cartographer Chapters 1-110.epub',
  chapter_from: 1,
  chapter_to: 110,
  skip_missing: false,
  format: 'epub',
  mime: 'application/epub+zip',
  state: 'waiting',
  progress: { downloaded: 84, total: 110 },
  blocked: { errors: [], not_queued: [] },
  error: null,
  waited: true,
  size_bytes: null,
  url: null,
  created_at: '2026-10-19T10:00:00.000Z',
  ready_at: null,
  ...overrides,
});

function mount(exports: Export[], response?: Response) {
  const fetchMock = vi.fn(() => Promise.resolve(response ?? new Response(null, { status: 204 })));
  vi.stubGlobal('fetch', fetchMock);
  const client: QueryClient = testClient();
  client.setQueryData(['exports', NOVEL], exports);
  // Not a live-ready test client: the panel must render cached data regardless of the gate.
  renderWithProviders(<ExportsPanel novelId={NOVEL} />, { client });
  return { fetchMock, client };
}

const call = (fetchMock: ReturnType<typeof vi.fn>, index = 0) => {
  const [url, init] = fetchMock.mock.calls[index] as [string, RequestInit];
  return {
    url,
    method: init.method,
    body: init.body === undefined ? undefined : (JSON.parse(init.body as string) as unknown),
  };
};

describe('ExportsPanel', () => {
  describe('layout', () => {
    it('is the exports anchor', () => {
      mount([makeExport()]);
      expect(document.getElementById('exports')).not.toBeNull();
    });

    it('shows one row per export in the given (newest first) order with filename and range', () => {
      mount([
        makeExport({ id: 'EX2', filename: 'b.epub', chapter_from: 5, chapter_to: 9 }),
        makeExport({ id: 'EX1', filename: 'a.epub' }),
      ]);
      const rows = screen.getAllByRole('listitem');
      expect(rows).toHaveLength(2);
      expect(within(rows[0] as HTMLElement).getByText('b.epub')).toBeInTheDocument();
      expect(within(rows[0] as HTMLElement).getByText('Chapters 5–9')).toBeInTheDocument();
      expect(within(rows[1] as HTMLElement).getByText('a.epub')).toBeInTheDocument();
    });
  });

  describe('waiting', () => {
    it('shows progress and Cancel, which deletes the export', async () => {
      const { fetchMock } = mount([makeExport()]);
      expect(screen.getByText('84 / 110 downloaded')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(call(fetchMock)).toMatchObject({
        url: `/api/v1/novels/${NOVEL}/exports/EX000000000001`,
        method: 'DELETE',
      });
      expect(await screen.findByText('Export cancelled')).toBeInTheDocument();
    });
  });

  describe('waiting and blocked', () => {
    it('shows "Blocked by N errors" and Retry, which re-queues those positions', async () => {
      const { fetchMock } = mount([makeExport({ blocked: { errors: [3, 9], not_queued: [] } })]);
      expect(screen.getByText('Blocked by 2 errors')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
      expect(call(fetchMock)).toEqual({
        url: `/api/v1/novels/${NOVEL}/chapters`,
        method: 'PATCH',
        body: { state: 'pending', chapters: [3, 9] },
      });
    });

    it('uses the singular for one error', () => {
      mount([makeExport({ blocked: { errors: [3], not_queued: [] } })]);
      expect(screen.getByText('Blocked by 1 error')).toBeInTheDocument();
    });

    it('shows "N chapters not queued" and Queue them', async () => {
      const { fetchMock } = mount([makeExport({ blocked: { errors: [], not_queued: [4, 5, 6] } })]);
      expect(screen.getByText('3 chapters not queued')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Queue them' }));
      expect(call(fetchMock).body).toEqual({ state: 'pending', chapters: [4, 5, 6] });
    });

    it('confirms with the queued count toast', async () => {
      mount(
        [makeExport({ blocked: { errors: [], not_queued: [4, 5] } })],
        Response.json({ updated: [4], skipped: [{ position: 5, state: 'downloading' }] }),
      );
      await userEvent.click(screen.getByRole('button', { name: 'Queue them' }));
      expect(await screen.findByText('Queued 1 · skipped 1')).toBeInTheDocument();
    });

    it('has no Retry or Queue them when nothing blocks it', () => {
      mount([makeExport()]);
      expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
      expect(screen.queryByRole('button', { name: 'Queue them' })).toBeNull();
    });
  });

  describe('building', () => {
    it('shows a spinner with Building… and no actions', () => {
      mount([makeExport({ state: 'building' })]);
      expect(screen.getByText('Building…')).toBeInTheDocument();
      expect(screen.queryByRole('button')).toBeNull();
    });
  });

  describe('ready', () => {
    const ready = makeExport({
      state: 'ready',
      size_bytes: 2_621_440,
      ready_at: '2026-10-19T11:00:00.000Z',
      url: '/static/exports/EX000000000001.epub',
    });

    it('shows size and date and a Download link to the url', () => {
      mount([ready]);
      expect(screen.getByText(/2\.5 MB/)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Download' })).toHaveAttribute(
        'href',
        '/static/exports/EX000000000001.epub',
      );
    });

    it('Delete removes the export', async () => {
      const { fetchMock } = mount([ready]);
      await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
      expect(call(fetchMock).method).toBe('DELETE');
      expect(await screen.findByText('Export deleted')).toBeInTheDocument();
    });
  });

  describe('failed', () => {
    const failed = makeExport({ state: 'failed', error: 'Range changed upstream' });

    it('shows the message', () => {
      mount([failed]);
      expect(screen.getByText('Range changed upstream')).toBeInTheDocument();
    });

    it('Try again creates a new export with the same range and format', async () => {
      const { fetchMock } = mount([failed], Response.json(makeExport(), { status: 201 }));
      await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
      expect(call(fetchMock)).toEqual({
        url: `/api/v1/novels/${NOVEL}/exports`,
        method: 'POST',
        body: { chapters: { start: 1, end: 110 }, format: 'epub', skip_missing: false },
      });
      expect(await screen.findByText('Export queued')).toBeInTheDocument();
    });

    it('Try again is disabled once the range is gone', () => {
      mount([makeExport({ state: 'failed', error: 'x', chapter_from: null, chapter_to: null })]);
      expect(screen.getByRole('button', { name: 'Try again' })).toBeDisabled();
    });

    it('Delete removes it', async () => {
      const { fetchMock } = mount([failed]);
      await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
      expect(call(fetchMock).method).toBe('DELETE');
    });
  });

  describe('on a failed action', () => {
    it('shows the error in a toast (export_building conflict)', async () => {
      mount(
        [makeExport()],
        Response.json(
          { error: { code: 'export_building', message: 'Export is building' } },
          { status: 409 },
        ),
      );
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(await screen.findByText('Export is building')).toBeInTheDocument();
    });
  });

  describe('live updates', () => {
    it('re-renders when the cache changes, including removal', async () => {
      const { client } = mount([makeExport()]);
      expect(screen.getByText('Cartographer Chapters 1-110.epub')).toBeInTheDocument();
      client.setQueryData(['exports', NOVEL], [makeExport({ state: 'building' })]);
      expect(await screen.findByText('Building…')).toBeInTheDocument();
      client.setQueryData(['exports', NOVEL], []);
      await vi.waitFor(() => {
        expect(screen.queryByText('Cartographer Chapters 1-110.epub')).toBeNull();
      });
    });
  });
});
