import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeNovel } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { NovelCard } from './NovelCard';

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubFetch(response: Response = Response.json({}, { status: 200 })) {
  const fetchMock = vi.fn(() => Promise.resolve(response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('NovelCard', () => {
  it('shows the cover image when there is one', () => {
    const { container } = renderWithProviders(
      <NovelCard novel={makeNovel({ cover_url: '/static/covers/a.jpg' })} />,
    );
    expect(container.querySelector('img')).toHaveAttribute('src', '/static/covers/a.jpg');
  });

  it('shows a placeholder with initials when there is no cover', () => {
    const { container } = renderWithProviders(<NovelCard novel={makeNovel()} />);
    expect(container.querySelector('img')).toBeNull();
    expect(screen.getByTestId('cover-placeholder')).toHaveTextContent('TN');
  });

  it('shows the title and the host', () => {
    renderWithProviders(<NovelCard novel={makeNovel()} />);
    expect(screen.getByText('The Ninth Cartographer')).toBeInTheDocument();
    expect(screen.getByText('royalroad.com')).toBeInTheDocument();
  });

  it('shows the host as title when the title is unknown', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ title: null })} />);
    expect(screen.getByRole('link', { name: 'royalroad.com' })).toBeInTheDocument();
  });

  it('shows a stacked bar and counts of downloaded, pending and error chapters', () => {
    renderWithProviders(
      <NovelCard
        novel={makeNovel({
          total_pages: 100,
          downloaded_pages: 50,
          pending_pages: 20,
          error_pages: 2,
        })}
      />,
    );
    const bar = screen.getByRole('img', { name: /50 of 100 downloaded/ });
    expect(bar).toHaveAccessibleName('50 of 100 downloaded, 20 pending, 2 errors');
    const widths = [...bar.children].map((c) => (c as HTMLElement).style.width);
    expect(widths).toEqual(['50%', '20%', '2%']);
    expect(screen.getByText('50 / 100 downloaded · 2 errors')).toBeInTheDocument();
  });

  it('says "no chapters yet" before any chapter is known', () => {
    renderWithProviders(<NovelCard novel={makeNovel()} />);
    expect(screen.getByText('no chapters yet')).toBeInTheDocument();
  });

  it('shows the "N new" badge when there are new chapters', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ new_pages: 3 })} />);
    expect(screen.getByText('3 new')).toBeInTheDocument();
  });

  it('shows no badge when there are no new chapters', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ new_pages: 0 })} />);
    expect(screen.queryByText(/ new$/)).toBeNull();
  });

  it('links to the novel page', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ id: 'NVabc' })} />);
    expect(screen.getByRole('link', { name: 'The Ninth Cartographer' })).toHaveAttribute(
      'href',
      '/novels/NVabc',
    );
  });

  it('shows "Syncing…" while syncing', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ syncing: true })} />);
    expect(screen.getByText('Syncing…')).toBeInTheDocument();
  });

  it('shows "Downloading…" while chapters are pending', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ total_pages: 5, pending_pages: 2 })} />);
    expect(screen.getByText('Downloading…')).toBeInTheDocument();
  });

  it('shows no activity indicator when idle', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ total_pages: 5, downloaded_pages: 5 })} />);
    expect(screen.queryByText(/Syncing|Downloading|Importing/)).toBeNull();
  });

  it('shows "Importing…" while importing', () => {
    renderWithProviders(<NovelCard novel={makeNovel({ importing: true })} />);
    expect(screen.getByText('Importing…')).toBeInTheDocument();
  });

  it('shows the import error with a Retry that sends POST sync', async () => {
    const fetchMock = stubFetch();
    renderWithProviders(
      <NovelCard novel={makeNovel({ id: 'NVabc', import_error: 'Site unreachable' })} />,
    );
    expect(screen.getByText('Import failed: Site unreachable')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/novels/NVabc/sync', { method: 'POST' });
    });
  });

  it('shows a toast when Retry fails', async () => {
    stubFetch(Response.json({ error: { code: 'not_found', message: 'gone' } }, { status: 404 }));
    renderWithProviders(<NovelCard novel={makeNovel({ import_error: 'x' })} />);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await within(screen.getByRole('status')).findByText('gone')).toBeInTheDocument();
  });

  it('follow toggle sends PATCH {followed} with the opposite value', async () => {
    const fetchMock = stubFetch();
    renderWithProviders(<NovelCard novel={makeNovel({ id: 'NVabc', followed: true })} />);
    await userEvent.click(screen.getByRole('button', { name: 'Following' }));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/novels/NVabc', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ followed: false }),
      });
    });
  });
});
