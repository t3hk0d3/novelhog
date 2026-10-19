import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeNovel } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { NovelHeader } from './NovelHeader';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

function stubFetch(response: Response = Response.json({})) {
  const fetchMock = vi.fn(() => Promise.resolve(response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

const bodyOf = (fetchMock: ReturnType<typeof stubFetch>, call = 0): unknown =>
  JSON.parse(((fetchMock.mock.calls[call] as unknown[])[1] as RequestInit).body as string);

describe('NovelHeader', () => {
  it('shows title, host, a TOC link and the cover', () => {
    const { container } = renderWithProviders(
      <NovelHeader novel={makeNovel({ cover_url: '/static/c.jpg' })} />,
    );
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('The Ninth Cartographer');
    expect(screen.getByText('royalroad.com')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Table of contents' })).toHaveAttribute(
      'href',
      'https://royalroad.com/fiction/1',
    );
    expect(container.querySelector('img')).toHaveAttribute('src', '/static/c.jpg');
  });

  it('shows the host as the title when the title is unknown', () => {
    renderWithProviders(<NovelHeader novel={makeNovel({ title: null })} />);
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('royalroad.com');
  });

  it('shows "checked … · next in …"', () => {
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-10-19T10:00:00.000Z') });
    renderWithProviders(
      <NovelHeader
        novel={makeNovel({
          last_refreshed_at: '2026-10-19T08:00:00.000Z',
          next_refresh_at: '2026-10-19T14:00:00.000Z',
        })}
      />,
    );
    expect(screen.getByText('checked 2 h ago · next in 4 h')).toBeInTheDocument();
  });

  it('shows counters', () => {
    renderWithProviders(
      <NovelHeader
        novel={makeNovel({
          total_pages: 630,
          downloaded_pages: 152,
          pending_pages: 25,
          error_pages: 2,
        })}
      />,
    );
    expect(screen.getByText('630 chapters · 152 downloaded · 25 pending · 2 errors')).toBeVisible();
  });

  describe('renaming', () => {
    it('sends PATCH {title} with the trimmed text on Enter', async () => {
      const fetchMock = stubFetch();
      renderWithProviders(<NovelHeader novel={makeNovel()} />);
      await userEvent.click(screen.getByRole('button', { name: 'Edit title' }));
      const input = screen.getByRole('textbox', { name: 'Title' });
      expect(input).toHaveValue('The Ninth Cartographer');
      await userEvent.clear(input);
      await userEvent.type(input, '  New name {Enter}');
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/novels/NV000000000001',
        expect.objectContaining({ method: 'PATCH' }),
      );
      expect(bodyOf(fetchMock)).toEqual({ title: 'New name' });
    });

    it('sends nothing on Escape or an empty title', async () => {
      const fetchMock = stubFetch();
      renderWithProviders(<NovelHeader novel={makeNovel()} />);
      await userEvent.click(screen.getByRole('button', { name: 'Edit title' }));
      await userEvent.type(screen.getByRole('textbox', { name: 'Title' }), 'x{Escape}');
      expect(screen.queryByRole('textbox')).toBeNull();
      await userEvent.click(screen.getByRole('button', { name: 'Edit title' }));
      await userEvent.clear(screen.getByRole('textbox', { name: 'Title' }));
      await userEvent.keyboard('{Enter}');
      expect(fetchMock).not.toHaveBeenCalled();
    });

    it('shows the error in a toast when the title is rejected', async () => {
      stubFetch(
        Response.json({ error: { code: 'invalid_title', message: 'Bad title' } }, { status: 422 }),
      );
      renderWithProviders(<NovelHeader novel={makeNovel()} />);
      await userEvent.click(screen.getByRole('button', { name: 'Edit title' }));
      await userEvent.type(screen.getByRole('textbox', { name: 'Title' }), 'x{Enter}');
      expect(await screen.findByText('Bad title')).toBeInTheDocument();
    });
  });

  it('toggles follow with PATCH {followed}', async () => {
    const fetchMock = stubFetch();
    renderWithProviders(<NovelHeader novel={makeNovel({ followed: true })} />);
    await userEvent.click(screen.getByRole('button', { name: /Following/ }));
    expect(bodyOf(fetchMock)).toEqual({ followed: false });
  });

  it('Sync sends POST /sync and is disabled while a sync runs', async () => {
    const fetchMock = stubFetch();
    const { unmount } = renderWithProviders(<NovelHeader novel={makeNovel()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Sync' }));
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/novels/NV000000000001/sync', {
      method: 'POST',
    });
    unmount();
    renderWithProviders(<NovelHeader novel={makeNovel({ syncing: true })} />);
    expect(screen.getByRole('button', { name: 'Syncing…' })).toBeDisabled();
  });

  it('closes the More disclosure on Escape', async () => {
    renderWithProviders(<NovelHeader novel={makeNovel()} />);
    await userEvent.click(screen.getByRole('button', { name: 'More' }));
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('button', { name: 'Delete novel…' })).toBeNull();
  });

  it('says "1 error" in the singular and "next check due" once the refresh is overdue', () => {
    vi.useFakeTimers({ toFake: ['Date'], now: new Date('2026-10-19T13:00:00.000Z') });
    renderWithProviders(
      <NovelHeader
        novel={makeNovel({ error_pages: 1, next_refresh_at: '2026-10-19T12:00:00.000Z' })}
      />,
    );
    expect(screen.getByText(/· 1 error$/)).toBeInTheDocument();
    expect(screen.getByText('next check due')).toBeInTheDocument();
  });

  it('More → Delete novel… opens the confirmation', async () => {
    renderWithProviders(<NovelHeader novel={makeNovel({ downloaded_pages: 3 })} />);
    await userEvent.click(screen.getByRole('button', { name: 'More' }));
    await userEvent.click(screen.getByRole('button', { name: 'Delete novel…' }));
    const dialog = screen.getByRole('dialog', { name: 'Delete novel' });
    expect(within(dialog).getByText(/The Ninth Cartographer/)).toBeInTheDocument();
  });

  describe('Export EPUB… in More', () => {
    it('calls onExport', async () => {
      const onExport = vi.fn();
      renderWithProviders(
        <NovelHeader novel={makeNovel({ total_pages: 5 })} onExport={onExport} />,
      );
      await userEvent.click(screen.getByRole('button', { name: 'More' }));
      await userEvent.click(screen.getByRole('button', { name: 'Export EPUB…' }));
      expect(onExport).toHaveBeenCalledOnce();
    });

    it('is disabled with zero chapters', async () => {
      renderWithProviders(<NovelHeader novel={makeNovel({ total_pages: 0 })} onExport={vi.fn()} />);
      await userEvent.click(screen.getByRole('button', { name: 'More' }));
      expect(screen.getByRole('button', { name: 'Export EPUB…' })).toBeDisabled();
    });
  });
});
