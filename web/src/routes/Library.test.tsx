import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { FakeEventSource } from '../test/FakeEventSource';
import { makeNovel } from '../test/fixtures';
import { testClient } from '../test/render';

// These tests count and script REST calls; the push state (and its /settings request) is not
// under test here.
vi.mock('../hooks/usePush', () => ({
  usePush: () => ({ status: 'hidden', busy: false, subscribe: vi.fn(), unsubscribe: vi.fn() }),
}));

beforeEach(() => {
  document.title = 'novelhog';
  localStorage.clear();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function mountApp(fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock);
  render(
    <MemoryRouter initialEntries={['/']}>
      <App queryClient={testClient()} />
    </MemoryRouter>,
  );
}

const respondWith = (novels: unknown[]) => vi.fn(() => Promise.resolve(Response.json(novels)));

function ready() {
  act(() => {
    FakeEventSource.instances[0]?.emit('ready');
  });
}

describe('Library (rendered through App at /)', () => {
  it('does not load novels before the stream is ready', () => {
    const fetchMock = respondWith([]);
    mountApp(fetchMock);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  describe('the browser tab title', () => {
    it('is just the app name until the novels have loaded', () => {
      mountApp(respondWith([makeNovel()]));
      expect(document.title).toBe('novelhog');
    });

    it('counts the novels: "3 novels · novelhog"', async () => {
      mountApp(
        respondWith([makeNovel({ id: 'NV1' }), makeNovel({ id: 'NV2' }), makeNovel({ id: 'NV3' })]),
      );
      ready();
      await waitFor(() => {
        expect(document.title).toBe('3 novels · novelhog');
      });
    });

    it('says "1 novel" for one', async () => {
      mountApp(respondWith([makeNovel()]));
      ready();
      await waitFor(() => {
        expect(document.title).toBe('1 novel · novelhog');
      });
    });

    it('says "0 novels" for an empty library', async () => {
      mountApp(respondWith([]));
      ready();
      await waitFor(() => {
        expect(document.title).toBe('0 novels · novelhog');
      });
    });
  });

  it('lists novel cards in the order the API returns them (newest first)', async () => {
    mountApp(
      respondWith([
        makeNovel({ id: 'NVnew', title: 'Newer' }),
        makeNovel({ id: 'NVold', title: 'Older' }),
      ]),
    );
    ready();
    const cards = await screen.findAllByRole('article');
    expect(cards.map((c) => within(c).getByRole('link').textContent)).toEqual(['Newer', 'Older']);
  });

  it('shows an empty state with an explanation and Add novel when there are none', async () => {
    mountApp(respondWith([]));
    ready();
    expect(await screen.findByText(/Your library is empty/)).toBeInTheDocument();
    await userEvent.click(
      within(screen.getByRole('main')).getByRole('button', { name: 'Add novel' }),
    );
    expect(screen.getByRole('dialog', { name: 'Add novel' })).toBeInTheDocument();
  });

  it('adds a card when a novel event arrives', async () => {
    mountApp(respondWith([]));
    ready();
    await screen.findByText(/Your library is empty/);
    act(() => {
      FakeEventSource.instances[0]?.emit('novel', makeNovel({ title: 'Live one' }));
    });
    expect(await screen.findByRole('link', { name: 'Live one' })).toBeInTheDocument();
  });

  it('shows an error message when loading fails', async () => {
    mountApp(
      vi.fn(() =>
        Promise.resolve(
          Response.json({ error: { code: 'internal_error', message: 'boom' } }, { status: 500 }),
        ),
      ),
    );
    ready();
    await waitFor(() => {
      expect(screen.getByRole('alert')).toHaveTextContent('boom');
    });
  });
});
