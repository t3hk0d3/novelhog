import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Route, Routes } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeNovel } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { DeleteNovelDialog } from './DeleteNovelDialog';

afterEach(() => {
  vi.unstubAllGlobals();
});

function open(response: Response = new Response(null, { status: 204 })) {
  const fetchMock = vi.fn(() => Promise.resolve(response));
  vi.stubGlobal('fetch', fetchMock);
  const onClose = vi.fn();
  renderWithProviders(
    <Routes>
      <Route path="/" element={<p>Library page</p>} />
      <Route
        path="/novels/:id"
        element={
          <DeleteNovelDialog
            novel={makeNovel({ id: 'NV1', downloaded_pages: 12 })}
            onClose={onClose}
          />
        }
      />
    </Routes>,
    { route: '/novels/NV1' },
  );
  return { fetchMock, onClose };
}

describe('DeleteNovelDialog', () => {
  it('names the novel and its downloaded chapter count', () => {
    open();
    const dialog = screen.getByRole('dialog', { name: 'Delete novel' });
    expect(dialog).toHaveTextContent('The Ninth Cartographer');
    expect(dialog).toHaveTextContent('12 downloaded chapters');
  });

  it('sends DELETE and goes to the library when confirmed', async () => {
    const { fetchMock } = open();
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/novels/NV1', { method: 'DELETE' });
    expect(await screen.findByText('Library page')).toBeInTheDocument();
  });

  it('sends nothing when cancelled', async () => {
    const { fetchMock, onClose } = open();
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows the error and stays on the page when the delete fails', async () => {
    open(Response.json({ error: { code: 'boom', message: 'Nope' } }, { status: 500 }));
    await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Nope');
    await waitFor(() => {
      expect(screen.queryByText('Library page')).toBeNull();
    });
  });
});
