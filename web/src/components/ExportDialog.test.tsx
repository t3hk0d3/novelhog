import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ChapterState } from '../api/client';
import { makeChapters } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { ExportDialog } from './ExportDialog';

const NOVEL = 'NV000000000001';

afterEach(() => {
  vi.unstubAllGlobals();
});

function setWide(wide: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: wide,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

function mount(
  count: number,
  states: Record<number, ChapterState>,
  { from = 1, to = count, response }: { from?: number; to?: number; response?: Response } = {},
) {
  setWide(true);
  const fetchMock = vi.fn(() =>
    Promise.resolve(response ?? Response.json({ id: 'EX000000000001' }, { status: 201 })),
  );
  vi.stubGlobal('fetch', fetchMock);
  const onClose = vi.fn();
  const onSubmitted = vi.fn();
  renderWithProviders(
    <ExportDialog
      novelId={NOVEL}
      novelTitle="Ashen Lantern Chronicles"
      chapters={makeChapters(count, states)}
      from={from}
      to={to}
      onClose={onClose}
      onSubmitted={onSubmitted}
    />,
  );
  return { fetchMock, onClose, onSubmitted };
}

const body = (fetchMock: ReturnType<typeof vi.fn>) =>
  JSON.parse((fetchMock.mock.calls[0] as [string, RequestInit])[1].body as string) as unknown;

/** Chapters from..to in `state`. */
const run = (from: number, to: number, state: ChapterState) =>
  Object.fromEntries(Array.from({ length: to - from + 1 }, (_, i) => [from + i, state]));

describe('ExportDialog', () => {
  describe('on open', () => {
    it('prefills From and To from the selection', () => {
      mount(30, {}, { from: 5, to: 20 });
      expect(screen.getByLabelText('From')).toHaveValue(5);
      expect(screen.getByLabelText('To')).toHaveValue(20);
    });

    it('shows a bar of the range states', () => {
      mount(10, { 3: 'error' });
      const bar = screen.getByRole('img', { name: /chapters 1–2 downloaded, 3 error/i });
      expect(bar).toBeInTheDocument();
    });

    it('shows format EPUB and the expected file name', () => {
      mount(30, {}, { from: 5, to: 20 });
      expect(screen.getByText('EPUB')).toBeInTheDocument();
      expect(screen.getByText('Ashen Lantern Chronicles Chapters 5-20.epub')).toBeInTheDocument();
    });

    it('updates the file name when the range changes', async () => {
      mount(30, {});
      await userEvent.clear(screen.getByLabelText('To'));
      await userEvent.type(screen.getByLabelText('To'), '12');
      expect(screen.getByText('Ashen Lantern Chronicles Chapters 1-12.epub')).toBeInTheDocument();
    });
  });

  describe('when all chapters are downloaded', () => {
    it('says so and offers Export', () => {
      mount(80, {});
      expect(screen.getByText('80 chapters, all downloaded')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Export' })).toBeEnabled();
      expect(screen.queryByLabelText('Skip missing chapters')).toBeNull();
    });

    it('submits the range with skip_missing false', async () => {
      const { fetchMock } = mount(80, {}, { from: 10, to: 20 });
      await userEvent.click(screen.getByRole('button', { name: 'Export' }));
      expect((fetchMock.mock.calls as unknown[][])[0]?.[0]).toBe(`/api/v1/novels/${NOVEL}/exports`);
      expect(body(fetchMock)).toEqual({
        chapters: { start: 10, end: 20 },
        format: 'epub',
        skip_missing: false,
      });
    });

    it('closes with the Export queued toast and reports the submit', async () => {
      const { onSubmitted, onClose } = mount(80, {});
      await userEvent.click(screen.getByRole('button', { name: 'Export' }));
      expect(await screen.findByText('Export queued')).toBeInTheDocument();
      expect(onSubmitted).toHaveBeenCalledOnce();
      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe('when chapters are missing', () => {
    it('lists the ranges to download first and counts errors', () => {
      mount(30, { ...run(21, 28, 'not_requested'), 29: 'error', 30: 'error' });
      const note = screen.getByText(/will be downloaded first/).closest('p');
      expect(note).toHaveTextContent(
        '10 chapters will be downloaded first (21–30, including 2 that failed before)',
      );
      expect(screen.getByRole('button', { name: 'Download & export' })).toBeEnabled();
    });

    it('submits without skip_missing and shows the Export queued toast', async () => {
      const { fetchMock } = mount(10, { 4: 'not_requested', 7: 'pending' });
      await userEvent.click(screen.getByRole('button', { name: 'Download & export' }));
      expect(body(fetchMock)).toEqual({
        chapters: { start: 1, end: 10 },
        format: 'epub',
        skip_missing: false,
      });
      expect(await screen.findByText('Export queued')).toBeInTheDocument();
    });
  });

  describe('Skip missing chapters', () => {
    it('explains what is left out and switches the button to Export now', async () => {
      mount(10, run(8, 10, 'not_requested'));
      await userEvent.click(screen.getByLabelText('Skip missing chapters'));
      expect(
        screen.getByText('Export the 7 downloaded chapters now; 3 are left out'),
      ).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Export now' })).toBeEnabled();
    });

    it('sends skip_missing true and shows the Exporting toast', async () => {
      const { fetchMock } = mount(10, run(8, 10, 'error'));
      await userEvent.click(screen.getByLabelText('Skip missing chapters'));
      await userEvent.click(screen.getByRole('button', { name: 'Export now' }));
      expect(body(fetchMock)).toEqual({
        chapters: { start: 1, end: 10 },
        format: 'epub',
        skip_missing: true,
      });
      expect(await screen.findByText('Exporting…')).toBeInTheDocument();
    });

    it('is disabled when nothing in the range is downloaded', () => {
      mount(10, run(1, 10, 'not_requested'));
      expect(screen.getByLabelText('Skip missing chapters')).toBeDisabled();
    });
  });

  describe('on an invalid range', () => {
    it.each([
      ['From above To', '8', '3', 'From must not be after To.'],
      ['To beyond the last chapter', '1', '99', 'Chapters run from 1 to 10.'],
      ['From below 1', '0', '5', 'Chapters run from 1 to 10.'],
    ])('%s shows inline validation and disables the button', async (_name, from, to, message) => {
      mount(10, {});
      await userEvent.clear(screen.getByLabelText('From'));
      await userEvent.type(screen.getByLabelText('From'), from);
      await userEvent.clear(screen.getByLabelText('To'));
      await userEvent.type(screen.getByLabelText('To'), to);
      expect(screen.getByRole('alert')).toHaveTextContent(message);
      expect(screen.getByRole('button', { name: 'Export' })).toBeDisabled();
    });

    it('an empty field is invalid too', async () => {
      mount(10, {});
      await userEvent.clear(screen.getByLabelText('From'));
      expect(screen.getByRole('button', { name: 'Export' })).toBeDisabled();
    });
  });

  describe('on a server error', () => {
    it('shows the message inline and stays open', async () => {
      const { onSubmitted } = mount(
        10,
        {},
        {
          response: Response.json(
            { error: { code: 'invalid_selector', message: 'Range is out of bounds' } },
            { status: 422 },
          ),
        },
      );
      await userEvent.click(screen.getByRole('button', { name: 'Export' }));
      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent('Range is out of bounds');
      });
      expect(onSubmitted).not.toHaveBeenCalled();
    });
  });

  describe('Cancel', () => {
    it('closes without submitting', async () => {
      const { onClose, fetchMock } = mount(10, {});
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).toHaveBeenCalledOnce();
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});
