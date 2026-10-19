import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeNovel } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { AddNovelDialog } from './AddNovelDialog';

afterEach(() => {
  vi.unstubAllGlobals();
});

function errorBody(code: string, message: string, details?: Record<string, unknown>) {
  return { error: { code, message, details } };
}

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(() => Promise.resolve(Response.json(body, { status })));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function submit(url = 'https://royalroad.com/fiction/1') {
  await userEvent.type(screen.getByLabelText('Table of contents URL'), url);
  await userEvent.click(screen.getByRole('button', { name: 'Add' }));
}

describe('AddNovelDialog', () => {
  it('is a modal dialog titled "Add novel"', () => {
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    expect(screen.getByRole('dialog', { name: 'Add novel' })).toHaveAttribute('aria-modal', 'true');
  });

  it('has the follow toggle on by default', () => {
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Following' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('keeps Add disabled until a URL is typed', async () => {
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled();
    await userEvent.type(screen.getByLabelText('Table of contents URL'), 'x');
    expect(screen.getByRole('button', { name: 'Add' })).toBeEnabled();
  });

  it('on 201 submits {toc_url, followed} and closes', async () => {
    const fetchMock = stubFetch(201, makeNovel());
    const onClose = vi.fn();
    renderWithProviders(<AddNovelDialog onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: 'Following' }));
    await submit();
    await waitFor(() => {
      expect(onClose).toHaveBeenCalledOnce();
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/novels', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ toc_url: 'https://royalroad.com/fiction/1', followed: false }),
    });
  });

  it('on 422 unsupported_host lists the supported hosts and stays open', async () => {
    stubFetch(
      422,
      errorBody('unsupported_host', 'Host not supported', {
        hosts: ['royalroad.com', 'fanmtl.com'],
      }),
    );
    const onClose = vi.fn();
    renderWithProviders(<AddNovelDialog onClose={onClose} />);
    await submit('https://nope.example/x');
    expect(await screen.findByText('Host not supported')).toBeInTheDocument();
    const items = screen.getAllByRole('listitem').map((li) => li.textContent);
    expect(items).toEqual(['royalroad.com', 'fanmtl.com']);
    expect(onClose).not.toHaveBeenCalled();
  });

  it('on 409 novel_exists links to the existing novel', async () => {
    stubFetch(409, errorBody('novel_exists', 'Already in library', { novel_id: 'NVold' }));
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    await submit();
    expect(await screen.findByRole('link', { name: 'Open the existing novel' })).toHaveAttribute(
      'href',
      '/novels/NVold',
    );
  });

  it('on 503 hosts_not_published asks to try again in a minute', async () => {
    stubFetch(503, errorBody('hosts_not_published', 'not ready'));
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    await submit();
    expect(
      await screen.findByText('The worker has not started yet; try again in a minute'),
    ).toBeInTheDocument();
  });

  it('on 422 invalid_url shows the server message', async () => {
    stubFetch(422, errorBody('invalid_url', 'That is not a URL'));
    renderWithProviders(<AddNovelDialog onClose={vi.fn()} />);
    await submit('nonsense');
    expect(await screen.findByText('That is not a URL')).toBeInTheDocument();
  });

  it('closes on Cancel and on Escape', async () => {
    const onClose = vi.fn();
    renderWithProviders(<AddNovelDialog onClose={onClose} />);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await userEvent.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
