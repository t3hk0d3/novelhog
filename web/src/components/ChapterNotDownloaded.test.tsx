import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeChapter } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { ChapterNotDownloaded } from './ChapterNotDownloaded';

const NOVEL = 'NV000000000001';

afterEach(() => {
  vi.unstubAllGlobals();
});

function show(state: Parameters<typeof makeChapter>[1] = {}, response?: Response) {
  const fetchMock = vi.fn(() =>
    Promise.resolve(response ?? Response.json({ updated: [3], skipped: [] })),
  );
  vi.stubGlobal('fetch', fetchMock);
  const chapter = makeChapter(3, state);
  renderWithProviders(<ChapterNotDownloaded novelId={NOVEL} chapter={chapter} />);
  return { fetchMock, chapter };
}

const card = () => screen.getByRole('region', { name: 'Chapter status' });
const patchBody = (fetchMock: ReturnType<typeof vi.fn>) =>
  JSON.parse((fetchMock.mock.calls[0] as [string, RequestInit])[1].body as string) as unknown;

describe('ChapterNotDownloaded', () => {
  describe.each([
    ['not_requested', 'Not requested', /Download it to read it here/],
    ['pending', 'Pending', /in the download queue/],
    ['downloading', 'Downloading', /being downloaded right now/],
    ['error', 'Error', /The last download failed/],
  ] as const)('a chapter that is %s', (state, label, explanation) => {
    it('says it is not downloaded yet, with a large icon of its state', () => {
      show({ state, error: 'boom' });
      expect(within(card()).getByText(/isn.t downloaded yet/)).toBeInTheDocument();
      const icons = card().querySelectorAll(`svg[data-state="${state}"]`);
      expect(icons.length).toBeGreaterThanOrEqual(2); // the large one and the one in the status pill
    });

    it('shows its status as a pronounced pill with an icon, once as text', () => {
      show({ state, error: 'boom' });
      const pill = screen.getByTestId('status-pill');
      expect(pill).toHaveTextContent(label);
      expect(pill.querySelector(`svg[data-state="${state}"]`)).not.toBeNull();
      expect(pill).toHaveClass('font-bold', 'rounded-full');
      expect(within(card()).getAllByText(label)).toHaveLength(1);
    });

    it('explains what happens next', () => {
      show({ state, error: 'boom' });
      expect(within(card()).getByText(explanation)).toBeInTheDocument();
    });

    it('is centred', () => {
      show({ state, error: 'boom' });
      expect(card()).toHaveClass('items-center', 'text-center');
    });
  });

  describe('not requested', () => {
    it('has a centred Download button that queues the chapter', async () => {
      const { fetchMock } = show({ state: 'not_requested' });
      const button = screen.getByRole('button', { name: 'Download' });
      expect(button).toHaveClass('justify-center');
      expect(button.querySelector('svg')).not.toBeNull();
      await userEvent.click(button);
      expect(patchBody(fetchMock)).toEqual({ state: 'pending', chapters: [3] });
    });
  });

  describe('error', () => {
    it('shows the error message and a Retry download button that queues it again', async () => {
      const { fetchMock } = show({ state: 'error', error: 'chapter 3 is unreadable' });
      expect(within(card()).getByText('chapter 3 is unreadable')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Retry download' }));
      expect(patchBody(fetchMock)).toEqual({ state: 'pending', chapters: [3] });
    });

    it('copes with no message', () => {
      show({ state: 'error', error: null });
      expect(screen.getByRole('button', { name: 'Retry download' })).toBeInTheDocument();
    });
  });

  describe('pending and downloading', () => {
    it.each(['pending', 'downloading'] as const)('has no button while it is %s', (state) => {
      show({ state });
      expect(screen.queryByRole('button')).toBeNull();
    });

    it('moves while it downloads, unless motion is reduced', () => {
      show({ state: 'downloading' });
      expect(screen.getByTestId('status-badge')).toHaveClass('motion-safe:animate-pulse');
    });
  });

  describe('when queueing fails', () => {
    it('shows the message and keeps the button', async () => {
      show(
        { state: 'not_requested' },
        Response.json({ error: { code: 'x', message: 'The host is paused' } }, { status: 409 }),
      );
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      await waitFor(() => {
        expect(screen.getByRole('alert')).toHaveTextContent('The host is paused');
      });
      expect(screen.getByRole('button', { name: 'Download' })).toBeEnabled();
    });
  });
});
