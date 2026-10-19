import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useEffect } from 'react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate, useNavigationType } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import type { Chapter, Novel } from '../api/client';
import { FakeEventSource } from '../test/FakeEventSource';
import { makeChapter, makeChapters, makeNovel } from '../test/fixtures';
import { testClient } from '../test/render';

beforeEach(() => {
  localStorage.clear();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

let goBack: () => void = () => undefined;

function Search() {
  const location = useLocation();
  const navigate = useNavigate();
  useEffect(() => {
    goBack = () => {
      void navigate(-1);
    };
  });
  return (
    <>
      <p data-testid="search">{location.search}</p>
      <p data-testid="nav">{useNavigationType()}</p>
    </>
  );
}

function mount(novel: Novel, chapters: Chapter[], search = '') {
  const fetchMock = vi.fn((input: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    if (input.includes('/chapters')) {
      return Promise.resolve(Response.json({ items: chapters, total: chapters.length }));
    }
    if (input.endsWith('/exports')) return Promise.resolve(Response.json([]));
    if (method === 'PATCH') return Promise.resolve(Response.json({ ...novel, new_pages: 0 }));
    return Promise.resolve(Response.json(novel));
  });
  vi.stubGlobal('fetch', fetchMock);
  render(
    <MemoryRouter initialEntries={[`/novels/${novel.id}${search}`]}>
      <Search />
      <App queryClient={testClient()} />
    </MemoryRouter>,
  );
  act(() => {
    FakeEventSource.instances[0]?.emit('ready');
  });
  return fetchMock;
}

const urlsOf = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.map(
    (c) => `${(c[1] as RequestInit | undefined)?.method ?? 'GET'} ${String(c[0])}`,
  );

describe('Novel page (rendered through App at /novels/:id)', () => {
  it('sends PATCH {seen: true} once on open', async () => {
    const fetchMock = mount(makeNovel(), makeChapters(3));
    await screen.findByRole('heading', { level: 1, name: 'The Ninth Cartographer' });
    await screen.findByText('Page 3');
    const patches = fetchMock.mock.calls.filter((c) => c[1]?.method === 'PATCH');
    expect(patches).toHaveLength(1);
    expect(JSON.parse((patches[0]?.[1] as RequestInit).body as string)).toEqual({ seen: true });
  });

  it('shows the chapters of the range holding the highest position by default', async () => {
    mount(makeNovel({ total_pages: 120 }), makeChapters(120));
    expect(await screen.findByRole('button', { name: '101–120', pressed: true })).toBeVisible();
    expect(screen.getByText('Page 120')).toBeInTheDocument();
    expect(screen.queryByText('Page 50')).toBeNull();
  });

  it('shows results from all ranges under range headings while a filter is active', async () => {
    mount(
      makeNovel({ total_pages: 120 }),
      makeChapters(120).map((c) =>
        c.position === 7 || c.position === 110 ? { ...c, state: 'error' as const } : c,
      ),
    );
    await userEvent.click(await screen.findByRole('button', { name: /^Error/ }));
    expect(screen.queryByRole('group', { name: 'Chapter ranges' })).toBeNull();
    const headings = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(['1–50', '101–120']);
    expect(screen.getByText('Page 7')).toBeInTheDocument();
    expect(screen.getByText('Page 110')).toBeInTheDocument();
  });

  it('searches by number or title', async () => {
    mount(makeNovel({ total_pages: 5 }), [
      ...makeChapters(4),
      makeChapter(5, { title: 'The Storm' }),
    ]);
    await userEvent.type(await screen.findByRole('searchbox'), 'storm');
    const list = screen.getByRole('region', { name: 'Chapters' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(1);
  });

  it('"New" shows the chapters released since the last visit, as counted before marking seen', async () => {
    const chapters = makeChapters(4).map((c) =>
      c.position > 2 ? { ...c, released_at: '2026-10-19T09:00:00.000Z' } : c,
    );
    mount(makeNovel({ total_pages: 4, new_pages: 2 }), chapters);
    await userEvent.click(await screen.findByRole('button', { name: 'New 2' }));
    const list = screen.getByRole('region', { name: 'Chapters' });
    expect(
      within(list)
        .getAllByRole('listitem')
        .map((li) => li.textContent),
    ).toEqual([expect.stringContaining('Page 3'), expect.stringContaining('Page 4')]);
  });

  it.each([
    ['2 new chapters at open', 2, 'New 3'],
    ['no new chapters at open', 0, 'New 1'],
  ])('"New" counts a chapter released during the visit (%s)', async (_name, newPages, label) => {
    const chapters = makeChapters(4).map((c) =>
      c.position > 2 ? { ...c, released_at: '2026-10-19T09:00:00.000Z' } : c,
    );
    mount(makeNovel({ total_pages: 4, new_pages: newPages }), chapters);
    await screen.findByText('Page 4');
    act(() => {
      FakeEventSource.instances[0]?.emit('chapter', {
        ...makeChapter(5, { released_at: '2026-10-19T11:00:00.000Z' }),
        novel_id: 'NV000000000001',
      });
    });
    await userEvent.click(await screen.findByRole('button', { name: label }));
    const items = within(screen.getByRole('region', { name: 'Chapters' })).getAllByRole('listitem');
    expect(items).toHaveLength(newPages === 2 ? 3 : 1);
    expect(items.at(-1)).toHaveTextContent('Page 5');
  });

  it('updates the overview bar when a chapter event arrives', async () => {
    mount(makeNovel({ total_pages: 3 }), makeChapters(3));
    const bar = await screen.findByRole('img', { name: /^Chapters 1–3 downloaded/ });
    act(() => {
      FakeEventSource.instances[0]?.emit('chapter', {
        ...makeChapter(3, { state: 'error' }),
        novel_id: 'NV000000000001',
      });
    });
    await waitFor(() => {
      expect(bar).toHaveAccessibleName('Chapters 1–2 downloaded, 3 error');
    });
  });

  it('shows "This novel was deleted" with a library link after a deleted novel event', async () => {
    const fetchMock = mount(makeNovel(), makeChapters(3));
    await screen.findByText('Page 3');
    const before = fetchMock.mock.calls.length;
    act(() => {
      FakeEventSource.instances[0]?.emit('novel', { id: 'NV000000000001', deleted: true });
    });
    expect(await screen.findByText('This novel was deleted')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Back to the library' })).toHaveAttribute('href', '/');
    expect(screen.queryByRole('heading', { level: 1, name: 'The Ninth Cartographer' })).toBeNull();
    await act(async () => {
      await Promise.resolve();
    });
    expect(urlsOf(fetchMock).slice(before)).toEqual([]);
  });

  describe('With zero chapters (RF-5)', () => {
    it('shows "No chapters yet"', async () => {
      mount(makeNovel(), []);
      expect(await screen.findByText('No chapters yet')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /^\d+–\d+$/ })).toBeNull();
    });

    it('shows the import state while importing', async () => {
      mount(makeNovel({ importing: true }), []);
      expect(await screen.findByText('Importing…')).toBeInTheDocument();
    });

    it('shows the import error when the import failed', async () => {
      mount(makeNovel({ import_error: 'timeout' }), []);
      expect(await screen.findByText('Import failed: timeout')).toBeInTheDocument();
    });
  });

  describe('select mode', () => {
    it('Select enters it, announces it and shows the toolbar and action bar', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      expect(screen.queryByRole('region', { name: 'Selection actions' })).toBeNull();
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      expect(screen.getByText('Select mode, 0 selected')).toBeInTheDocument();
      expect(screen.getByText('0 selected', { selector: 'p' })).toBeInTheDocument();
      expect(screen.getByRole('region', { name: 'Selection actions' })).toBeInTheDocument();
      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });

    it('Done and Escape leave it and clear the selection', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      expect(screen.getAllByText(/1 selected/).length).toBeGreaterThan(0);
      await userEvent.click(screen.getByRole('button', { name: 'Done' }));
      expect(screen.queryByRole('listbox')).toBeNull();
      await userEvent.click(screen.getByRole('button', { name: 'Select' }));
      expect(screen.getByText('0 selected', { selector: 'p' })).toBeInTheDocument();
      await userEvent.keyboard('{Escape}');
      expect(screen.queryByRole('listbox')).toBeNull();
    });

    it('Select all covers the rows matching the search, not the hidden ones', async () => {
      mount(makeNovel({ total_pages: 4 }), [
        ...makeChapters(3),
        makeChapter(4, { title: 'The Storm' }),
      ]);
      await userEvent.type(await screen.findByRole('searchbox'), 'page');
      await userEvent.click(screen.getByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('button', { name: 'Select all' }));
      expect(screen.getByText('3 selected', { selector: 'p' })).toBeInTheDocument();
    });

    it('keeps a selected chapter selected when a live event updates it', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      act(() => {
        FakeEventSource.instances[0]?.emit('chapter', {
          ...makeChapter(2, { state: 'pending' }),
          novel_id: 'NV000000000001',
        });
      });
      const row = await screen.findByRole('option', { name: 'Page 2, Pending' });
      expect(row).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByText(/2–2 · 1 queued/)).toBeInTheDocument();
    });

    it('a bulk action sends one PATCH, shows the toast and leaves select mode', async () => {
      const fetchMock = mount(
        makeNovel({ total_pages: 3 }),
        makeChapters(3, { 2: 'not_requested' }),
      );
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      fetchMock.mockImplementationOnce(() =>
        Promise.resolve(Response.json({ updated: [2], skipped: [] })),
      );
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      expect(await screen.findByText('Queued 1 · skipped 0')).toBeInTheDocument();
      expect(screen.queryByRole('listbox')).toBeNull();
      expect(urlsOf(fetchMock).at(-1)).toBe('PATCH /api/v1/novels/NV000000000001/chapters');
    });

    describe('focus', () => {
      it('moves to the selection heading on entering and back to Select on Done', async () => {
        mount(makeNovel({ total_pages: 3 }), makeChapters(3));
        await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
        expect(screen.getByText('0 selected', { selector: 'p' })).toHaveFocus();
        await userEvent.click(screen.getByRole('button', { name: 'Done' }));
        expect(screen.getByRole('button', { name: 'Select' })).toHaveFocus();
      });

      it('moves to the row that started the mode and returns to it on Escape', async () => {
        mount(makeNovel({ total_pages: 3 }), makeChapters(3));
        await screen.findByText('Page 2');
        fireEvent.click(screen.getByRole('link', { name: /^Page 2,/ }), { shiftKey: true });
        expect(screen.getByRole('option', { name: /^Page 2,/ })).toHaveFocus();
        await userEvent.keyboard('{Escape}');
        expect(screen.getByRole('link', { name: /^Page 2,/ })).toHaveFocus();
      });

      it('returns to the Select button after a finished bulk action', async () => {
        const fetchMock = mount(
          makeNovel({ total_pages: 3 }),
          makeChapters(3, { 2: 'not_requested' }),
        );
        await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
        await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
        fetchMock.mockImplementationOnce(() =>
          Promise.resolve(Response.json({ updated: [2], skipped: [] })),
        );
        await userEvent.click(screen.getByRole('button', { name: 'Download' }));
        await screen.findByText('Queued 1 · skipped 0');
        expect(screen.getByRole('button', { name: 'Select' })).toHaveFocus();
      });
    });
  });

  describe('chapter details', () => {
    const search = () => screen.getByTestId('search').textContent;

    it('clicking a row opens details for it and puts ?chapter=<id> in the URL', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('link', { name: /^Page 2,/ }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: 'Chapter 2 of 3' })).toBeInTheDocument();
      expect(search()).toBe('?chapter=CH000000000002');
    });

    it('opens details from a ?chapter=<id> deep link once the chapters load', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3), '?chapter=CH000000000003');
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('heading', { name: 'Chapter 3 of 3' })).toBeInTheDocument();
    });

    it('drops the parameter for an unknown chapter and opens nothing', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3), '?chapter=CH999999999999');
      await screen.findByText('Page 3');
      await waitFor(() => {
        expect(search()).toBe('');
      });
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('closing removes the parameter and returns focus to the row', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      const row = await screen.findByRole('link', { name: /^Page 2,/ });
      row.focus();
      await userEvent.click(row);
      await userEvent.click(await screen.findByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(search()).toBe('');
      expect(row).toHaveFocus();
    });

    it('open then close pops the pushed entry, so Back goes to the previous page', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('link', { name: /^Page 2,/ }));
      await userEvent.click(await screen.findByRole('button', { name: 'Close' }));
      await waitFor(() => {
        expect(screen.queryByRole('dialog')).toBeNull();
      });
      expect(search()).toBe('');
      expect(screen.getByTestId('nav')).toHaveTextContent('POP');
    });

    it('a deep link closes by replace', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3), '?chapter=CH000000000003');
      await userEvent.click(await screen.findByRole('button', { name: 'Close' }));
      expect(search()).toBe('');
      expect(screen.getByTestId('nav')).toHaveTextContent('REPLACE');
    });

    it('Back while open closes the dialog', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('link', { name: /^Page 2,/ }));
      await screen.findByRole('dialog');
      act(() => {
        goBack();
      });
      await waitFor(() => {
        expect(screen.queryByRole('dialog')).toBeNull();
      });
      expect(search()).toBe('');
    });

    it('Next steps to the following row and updates the parameter', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('link', { name: /^Page 2,/ }));
      await userEvent.click(await screen.findByRole('button', { name: 'Next chapter' }));
      expect(screen.getByRole('heading', { name: 'Chapter 3 of 3' })).toBeInTheDocument();
      expect(search()).toBe('?chapter=CH000000000003');
    });

    it('shows a live update of the open chapter', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('link', { name: /^Page 2,/ }));
      const dialog = await screen.findByRole('dialog');
      expect(within(dialog).getByRole('button', { name: 'Download again' })).toBeInTheDocument();
      act(() => {
        FakeEventSource.instances[0]?.emit('chapter', {
          ...makeChapter(2, { state: 'pending' }),
          novel_id: 'NV000000000001',
        });
      });
      expect(await within(dialog).findByRole('button', { name: 'Cancel' })).toBeInTheDocument();
    });

    it('does not open in select mode: a click toggles the row', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      expect(screen.queryByRole('dialog')).toBeNull();
      expect(search()).toBe('');
    });
  });

  describe('exports', () => {
    it('renders the exports panel from the listing under the #exports anchor', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await screen.findByText('Page 3');
      expect(document.getElementById('exports')).not.toBeNull();
    });

    it('the header menu opens the dialog for the whole novel and Cancel closes it', async () => {
      mount(makeNovel({ total_pages: 3 }), makeChapters(3));
      await screen.findByText('Page 3');
      await userEvent.click(screen.getByRole('button', { name: 'More' }));
      await userEvent.click(screen.getByRole('button', { name: 'Export EPUB…' }));
      const dialog = screen.getByRole('dialog', { name: 'Export' });
      expect(within(dialog).getByLabelText('From')).toHaveValue(1);
      expect(within(dialog).getByLabelText('To')).toHaveValue(3);
      await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('dialog', { name: 'Export' })).toBeNull();
    });

    it('the action bar opens it with the selection; Cancel keeps select mode', async () => {
      mount(makeNovel({ total_pages: 4 }), makeChapters(4));
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 3,/ }));
      await userEvent.click(screen.getByRole('button', { name: 'Export EPUB…' }));
      const dialog = screen.getByRole('dialog', { name: 'Export' });
      expect(within(dialog).getByLabelText('From')).toHaveValue(2);
      expect(within(dialog).getByLabelText('To')).toHaveValue(3);
      await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });

    it('submitting ends select mode and clears the selection', async () => {
      const fetchMock = mount(makeNovel({ total_pages: 4 }), makeChapters(4));
      await userEvent.click(await screen.findByRole('button', { name: 'Select' }));
      await userEvent.click(screen.getByRole('option', { name: /^Page 2,/ }));
      await userEvent.click(screen.getByRole('button', { name: 'Export EPUB…' }));
      await userEvent.click(
        within(screen.getByRole('dialog', { name: 'Export' })).getByRole('button', {
          name: 'Export',
        }),
      );
      await waitFor(() => {
        expect(screen.queryByRole('listbox')).toBeNull();
      });
      expect(screen.queryByRole('dialog', { name: 'Export' })).toBeNull();
      expect(urlsOf(fetchMock)).toContain('POST /api/v1/novels/NV000000000001/exports');
    });
  });
});
