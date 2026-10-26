import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { act } from 'react';
import { Route, Routes, useLocation } from 'react-router';
import type { QueryClient } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Chapter } from '../api/client';
import { READER_SETTINGS_KEY } from '../hooks/useReaderSettings';
import { POSITIONS_KEY, savePosition } from '../lib/readingPosition';
import { makeChapter, makeChapters } from '../test/fixtures';
import { renderWithProviders, testClient } from '../test/render';
import { Reader } from './Reader';

const NOVEL = 'NV000000000001';

interface Options {
  chapters?: Chapter[];
  /** The position the page opens at. */
  position?: number;
  wide?: boolean;
  reducedMotion?: boolean;
  /** Fails every content request with this message. */
  contentError?: string;
  /** Start with the chapter list in the cache (a visit from the novel page); false is a direct link. */
  preload?: boolean;
}

function stubMedia(wide: boolean, reducedMotion: boolean) {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: query.includes('min-width')
        ? wide
        : query.includes('reduced-motion') && reducedMotion,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

function Where() {
  return <output data-testid="where">{useLocation().pathname}</output>;
}

/** Serves the chapter list, content, and PATCH /chapters; returns the fetch mock. */
function stubApi(state: { chapters: Chapter[] }, contentError?: string) {
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === 'PATCH') {
      return Promise.resolve(Response.json({ updated: [1], skipped: [] }));
    }
    if (url.includes('/content')) {
      const id = url.split('/chapters/')[1]?.split('/')[0] ?? '';
      if (contentError !== undefined) {
        return Promise.resolve(
          Response.json({ error: { code: 'not_found', message: contentError } }, { status: 404 }),
        );
      }
      return Promise.resolve(Response.json({ markdown: `Story text of ${id}` }));
    }
    return Promise.resolve(Response.json({ items: state.chapters, total: state.chapters.length }));
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function mount({
  chapters = makeChapters(5),
  position = 3,
  wide = true,
  reducedMotion = false,
  contentError,
  preload = true,
}: Options = {}) {
  stubMedia(wide, reducedMotion);
  const state = { chapters };
  const fetchMock = stubApi(state, contentError);
  const client: QueryClient = testClient();
  // The list is the test's to change (see `update`); a refetch on ready would race it.
  client.setQueryDefaults(['chapters', NOVEL], { staleTime: Infinity });
  if (preload) client.setQueryData(['chapters', NOVEL], chapters);
  const open = chapters.find((c) => c.position === position) ?? makeChapter(position);
  const view = renderWithProviders(
    <>
      <Where />
      <Routes>
        <Route path="/novels/:id/chapters/:chapterId" element={<Reader />} />
      </Routes>
    </>,
    { client, route: `/novels/${NOVEL}/chapters/${open.id}`, live: true },
  );
  /** What a live event does: the server's list and the cache change together. */
  const update = (next: Chapter[]) => {
    state.chapters = next;
    act(() => {
      client.setQueryData(['chapters', NOVEL], next);
    });
  };
  return { fetchMock, client, chapters, view, update };
}

const idOf = (position: number) => makeChapter(position).id;
const where = () => screen.getByTestId('where').textContent;
const contentCalls = (fetchMock: ReturnType<typeof vi.fn>) =>
  fetchMock.mock.calls.filter(([url]) => String(url).includes('/content'));

let scrollTo = vi.fn();

beforeEach(() => {
  localStorage.clear();
  document.title = 'novelhog';
  scrollTo = vi.fn();
  vi.stubGlobal('scrollTo', scrollTo);
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, value: 400 });
});

afterEach(() => {
  vi.unstubAllGlobals();
  Reflect.deleteProperty(HTMLElement.prototype, 'clientWidth');
});

describe('Reader', () => {
  describe('downloaded chapter', () => {
    it('shows the title, "N of M" and the rendered content', async () => {
      mount({
        chapters: makeChapters(5).map((c) => (c.position === 3 ? { ...c, title: 'The Gate' } : c)),
      });
      expect(await screen.findByText(`Story text of ${idOf(3)}`)).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: 'The Gate' })).toBeInTheDocument();
      expect(within(screen.getByRole('article')).getByText('3 of 5')).toBeInTheDocument();
      expect(screen.getByRole('progressbar', { name: 'Reading position' })).toHaveAttribute(
        'aria-valuetext',
        'Chapter 3 of 5',
      );
    });

    it('falls back to "Chapter N" when the chapter has no title', async () => {
      mount({ chapters: [makeChapter(1, { title: null })], position: 1 });
      expect(await screen.findByRole('heading', { name: 'Chapter 1' })).toBeInTheDocument();
    });

    it('sends one content request', async () => {
      const { fetchMock } = mount({ chapters: [makeChapter(1)], position: 1 });
      await screen.findByText(`Story text of ${idOf(1)}`);
      expect(contentCalls(fetchMock)).toHaveLength(1);
    });

    it('prefetches the downloaded neighbours once the text is in', async () => {
      const { fetchMock } = mount({ chapters: makeChapters(5, { 4: 'pending' }) });
      await screen.findByText(`Story text of ${idOf(3)}`);
      await waitFor(() => {
        expect(contentCalls(fetchMock).map(([url]) => String(url))).toEqual(
          expect.arrayContaining([expect.stringContaining(`/chapters/${idOf(2)}/content`)]),
        );
      });
      const urls = contentCalls(fetchMock).map(([url]) => String(url));
      expect(urls.some((url) => url.includes(`/chapters/${idOf(4)}/content`))).toBe(false);
    });

    it('sets the document title to the chapter title', async () => {
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      expect(document.title).toContain('Page 3');
    });
  });

  describe('loading', () => {
    it('shows a loading state', () => {
      mount();
      expect(screen.getByText('Loading content…')).toBeInTheDocument();
    });
  });

  describe('chapter not downloaded', () => {
    it('says so, shows its state and a Download action that queues it', async () => {
      const { fetchMock } = mount({ chapters: makeChapters(5, { 3: 'not_requested' }) });
      expect(screen.getByText(/isn.t downloaded yet/)).toBeInTheDocument();
      expect(screen.getByText('Not requested')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Download' }));
      const patch = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH');
      expect(JSON.parse((patch?.[1] as RequestInit).body as string)).toEqual({
        state: 'pending',
        chapters: [3],
      });
      expect(contentCalls(fetchMock)).toHaveLength(0);
    });

    it('loads and shows the content when it becomes downloaded (live)', async () => {
      const chapters = makeChapters(5, { 3: 'pending' });
      const { update } = mount({ chapters });
      expect(screen.queryByText(`Story text of ${idOf(3)}`)).toBeNull();
      update(
        chapters.map((c) =>
          c.position === 3
            ? { ...c, state: 'downloaded', updated_at: '2026-10-02T10:00:00.000Z' }
            : c,
        ),
      );
      expect(await screen.findByText(`Story text of ${idOf(3)}`)).toBeInTheDocument();
    });
  });

  describe('content request fails', () => {
    it('shows the error message and a retry action', async () => {
      const { fetchMock } = mount({ contentError: 'Content is gone' });
      expect(await screen.findByText('Content is gone')).toBeInTheDocument();
      const before = contentCalls(fetchMock).length;
      await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
      await waitFor(() => {
        expect(contentCalls(fetchMock).length).toBeGreaterThan(before);
      });
    });
  });

  describe('chapter id not in the novel', () => {
    it('says the chapter no longer exists and links back to the novel and the library', () => {
      stubMedia(true, false);
      stubApi({ chapters: makeChapters(2) });
      const client = testClient();
      client.setQueryData(['chapters', NOVEL], makeChapters(2));
      renderWithProviders(
        <Routes>
          <Route path="/novels/:id/chapters/:chapterId" element={<Reader />} />
        </Routes>,
        { client, route: `/novels/${NOVEL}/chapters/CHmissing`, live: true },
      );
      expect(screen.getByText('This chapter no longer exists')).toBeInTheDocument();
      expect(screen.getByRole('link', { name: 'Back to the novel' })).toHaveAttribute(
        'href',
        `/novels/${NOVEL}`,
      );
      expect(screen.getByRole('link', { name: 'Library' })).toHaveAttribute('href', '/');
    });
  });

  describe('Previous / Next', () => {
    it('navigate to the neighbouring chapter in position order', async () => {
      mount();
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      await userEvent.click(screen.getByRole('button', { name: 'Previous chapter' }));
      await userEvent.click(screen.getByRole('button', { name: 'Previous chapter' }));
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(2)}`);
    });

    it('Previous is disabled on the first chapter and Next on the last', () => {
      mount({ position: 1 });
      expect(screen.getByRole('button', { name: 'Previous chapter' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Next chapter' })).toBeEnabled();
    });

    it('Next is disabled on the last chapter', () => {
      mount({ position: 5 });
      expect(screen.getByRole('button', { name: 'Next chapter' })).toBeDisabled();
    });

    it('the arrow keys do the same', async () => {
      mount();
      await userEvent.keyboard('{ArrowRight}');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      await userEvent.keyboard('{ArrowLeft}{ArrowLeft}');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(2)}`);
    });

    it('ignores the arrow keys with Ctrl, Alt or Meta held', async () => {
      mount();
      await userEvent.keyboard('{Control>}{ArrowRight}{/Control}');
      await userEvent.keyboard('{Alt>}{ArrowRight}{/Alt}');
      await userEvent.keyboard('{Meta>}{ArrowRight}{/Meta}');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
    });

    it('ignores the arrow keys while typing in an input', () => {
      mount();
      const input = document.createElement('input');
      document.body.append(input);
      input.focus();
      fireEvent.keyDown(input, { key: 'ArrowRight' });
      input.remove();
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
    });

    it('scrolls to the top of the new chapter', async () => {
      mount();
      scrollTo.mockClear();
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      expect(scrollTo).toHaveBeenCalledWith(0, 0);
    });
  });

  describe('settings', () => {
    const column = () => screen.getByRole('article');

    it('changing the font size restyles the content and persists across a reload', async () => {
      mount();
      const before = column().className;
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      await userEvent.click(screen.getByRole('button', { name: 'Extra large text' }));
      expect(column().className).not.toBe(before);
      expect(JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) ?? '{}')).toMatchObject({
        fontSize: 'xlarge',
      });
    });

    it('changing the line width restyles the content', async () => {
      mount();
      const before = column().className;
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      await userEvent.click(screen.getByRole('button', { name: 'Wide' }));
      expect(column().className).not.toBe(before);
    });

    it('opens in a centred modal dialog on desktop, with the line width', async () => {
      mount({ wide: true });
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      const modal = screen.getByRole('dialog', { name: 'Reading settings' });
      expect(modal).toHaveClass('m-auto');
      expect(modal).toHaveClass('max-w-3xl');
      expect(modal).not.toHaveClass('mt-auto');
      expect(within(modal).getByRole('button', { name: 'Wide' })).toBeInTheDocument();
    });

    it('closes with its × and with Escape', async () => {
      mount({ wide: true });
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      await userEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(screen.queryByRole('dialog')).toBeNull();
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      await userEvent.keyboard('{Escape}');
      expect(screen.queryByRole('dialog')).toBeNull();
    });

    it('does not turn chapters with the arrow keys while it is open', async () => {
      mount({ wide: true });
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      await userEvent.keyboard('{ArrowRight}');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
    });

    it('opens in a bottom sheet on a phone, with only the font size', async () => {
      mount({ wide: false });
      await userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));
      const sheet = screen.getByRole('dialog');
      expect(sheet).toHaveClass('mt-auto');
      expect(within(sheet).getByRole('button', { name: 'Large text' })).toBeInTheDocument();
      expect(within(sheet).queryByRole('button', { name: 'Wide' })).toBeNull();
    });
  });

  describe('reading position', () => {
    // A page 3000 px tall in a 1000 px window: 2000 px of scrolling, so ratio r is at y = 2000 r.
    function page(y = 0) {
      Object.defineProperty(window, 'scrollY', { configurable: true, value: y });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 1000 });
      Object.defineProperty(document.documentElement, 'scrollHeight', {
        configurable: true,
        value: 3000,
      });
    }
    function scrollPage(y: number) {
      page(y);
      window.dispatchEvent(new Event('scroll'));
    }
    const positions = () =>
      JSON.parse(localStorage.getItem(POSITIONS_KEY) ?? '{}') as Record<string, [number, number]>;
    const scrollTo_calls_clear = () => {
      scrollTo.mockClear();
    };

    beforeEach(() => {
      page(0);
    });

    afterEach(() => {
      Reflect.deleteProperty(window, 'scrollY');
      Reflect.deleteProperty(window, 'innerHeight');
      Reflect.deleteProperty(document.documentElement, 'scrollHeight');
    });

    it('opens a chapter where it was left, once its text is in', async () => {
      savePosition(idOf(3), 0.5);
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      expect(scrollTo).toHaveBeenLastCalledWith(0, 1000);
    });

    it('opens a chapter with no saved position at the top', async () => {
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      expect(scrollTo).toHaveBeenLastCalledWith(0, 0);
    });

    it('opens a chapter that was read to the end at the top again', async () => {
      savePosition(idOf(3), 0.99);
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      expect(scrollTo).toHaveBeenLastCalledWith(0, 0);
    });

    it('saves where you are as you scroll', async () => {
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      scrollPage(500);
      await waitFor(() => {
        expect(positions()[idOf(3)]?.[0]).toBeCloseTo(0.25, 3);
      });
    });

    it('does not save while the text has not loaded', async () => {
      mount();
      scrollPage(800);
      await screen.findByText(`Story text of ${idOf(3)}`);
      await new Promise((resolve) => setTimeout(resolve, 600));
      expect(positions()[idOf(3)]).toBeUndefined();
    });

    it('keeps the position of the chapter you leave, not where the page was squeezed to', async () => {
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      scrollPage(1000);
      await userEvent.click(screen.getByRole('button', { name: 'Next chapter' }));
      scrollPage(0); // the page got short while the next chapter loads, and the browser clamped it
      await screen.findByText(`Story text of ${idOf(4)}`);
      await new Promise((resolve) => setTimeout(resolve, 600));
      expect(positions()[idOf(3)]?.[0]).toBeCloseTo(0.5, 3);
    });

    it('saves when the page is hidden, without waiting', async () => {
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      scrollPage(1000);
      window.dispatchEvent(new Event('pagehide'));
      expect(positions()[idOf(3)]?.[0]).toBeCloseTo(0.5, 3);
    });

    it('puts you back after an image above the spot has loaded and pushed the text down', async () => {
      savePosition(idOf(3), 0.5);
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      scrollTo_calls_clear();
      const image = document.createElement('img');
      screen.getByRole('article').append(image);
      image.dispatchEvent(new Event('load'));
      expect(scrollTo).toHaveBeenLastCalledWith(0, 1000);
    });

    it('leaves you alone once you have scrolled yourself', async () => {
      savePosition(idOf(3), 0.5);
      mount();
      await screen.findByText(`Story text of ${idOf(3)}`);
      window.dispatchEvent(new Event('wheel'));
      scrollTo_calls_clear();
      const image = document.createElement('img');
      screen.getByRole('article').append(image);
      image.dispatchEvent(new Event('load'));
      expect(scrollTo).not.toHaveBeenCalled();
    });
  });

  describe('the bottom bar while reading down the page', () => {
    // A page 5000 px tall in a 1000 px window.
    function pageAt(y: number) {
      Object.defineProperty(window, 'scrollY', { configurable: true, value: y });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 1000 });
      Object.defineProperty(document.documentElement, 'scrollHeight', {
        configurable: true,
        value: 5000,
      });
    }
    function scrollTo(y: number) {
      pageAt(y);
      act(() => {
        window.dispatchEvent(new Event('scroll'));
      });
    }
    const bar = () => screen.getByRole('navigation', { name: 'Chapters' });

    afterEach(() => {
      Reflect.deleteProperty(window, 'scrollY');
      Reflect.deleteProperty(window, 'innerHeight');
      Reflect.deleteProperty(document.documentElement, 'scrollHeight');
    });

    it('is in place at first', () => {
      pageAt(0);
      mount({ wide: false });
      expect(bar()).not.toHaveClass('translate-y-full');
    });

    it('slides away when the reader scrolls down, and comes back on scrolling up', () => {
      pageAt(200);
      mount({ wide: false });
      scrollTo(210);
      scrollTo(240);
      expect(bar()).toHaveClass('translate-y-full');
      scrollTo(225);
      expect(bar()).not.toHaveClass('translate-y-full');
    });

    it('stays in place on a desktop-width screen', () => {
      pageAt(200);
      mount({ wide: true });
      scrollTo(210);
      scrollTo(300);
      expect(bar()).not.toHaveClass('translate-y-full');
    });

    it('is there at the very end of the text', () => {
      pageAt(200);
      mount({ wide: false });
      scrollTo(300);
      expect(bar()).toHaveClass('translate-y-full');
      scrollTo(4000);
      expect(bar()).not.toHaveClass('translate-y-full');
    });

    it('is not hidden by the page jumping to a remembered spot', () => {
      pageAt(0);
      mount({ wide: false });
      scrollTo(2500);
      expect(bar()).not.toHaveClass('translate-y-full');
    });

    it('is back in place for the next chapter', async () => {
      pageAt(200);
      mount({ wide: false });
      scrollTo(210);
      scrollTo(260);
      expect(bar()).toHaveClass('translate-y-full');
      await userEvent.click(within(bar()).getByRole('button', { name: 'Next chapter' }));
      expect(bar()).not.toHaveClass('translate-y-full');
    });
  });

  describe('theme and font', () => {
    const root = () => document.documentElement;
    const openSettings = () =>
      userEvent.click(screen.getByRole('button', { name: 'Reading settings' }));

    afterEach(() => {
      root().removeAttribute('style');
    });

    it('follows the app theme at first: the page is not recoloured', () => {
      mount();
      expect(root().style.getPropertyValue('--page')).toBe('');
    });

    it('picking a theme recolours the whole page and the choice is remembered', async () => {
      mount();
      await openSettings();
      await userEvent.click(screen.getByRole('button', { name: 'Sepia' }));
      expect(root().style.getPropertyValue('--page')).toBe('#f4ecd8');
      expect(root().style.getPropertyValue('--ink')).toBe('#433422');
      expect(JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) ?? '{}')).toMatchObject({
        themeId: 'sepia',
      });
    });

    it('picking Match app puts the app colours back', async () => {
      mount();
      await openSettings();
      await userEvent.click(screen.getByRole('button', { name: 'Sepia' }));
      await userEvent.click(screen.getByRole('button', { name: 'Match app' }));
      expect(root().style.getPropertyValue('--page')).toBe('');
    });

    it('applies the stored theme when the page opens, and removes it on leaving', () => {
      localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify({ themeId: 'night' }));
      const { view } = mount();
      expect(root().style.getPropertyValue('--page')).toBe('#14161a');
      view.unmount();
      expect(root().style.getPropertyValue('--page')).toBe('');
      expect(root().style.getPropertyValue('--ink')).toBe('');
    });

    it('applies a custom theme the user saved', async () => {
      mount();
      await openSettings();
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      await userEvent.type(screen.getByLabelText('Theme name'), 'Mine');
      fireEvent.change(screen.getByLabelText('Background colour'), {
        target: { value: '#102030' },
      });
      await userEvent.click(screen.getByRole('button', { name: 'Save theme' }));
      expect(root().style.getPropertyValue('--page')).toBe('#102030');
    });

    it('sets the reading font on the text, not on the controls', async () => {
      mount();
      expect(screen.getByRole('article').style.fontFamily).toContain('system-ui');
      await openSettings();
      await userEvent.click(screen.getByRole('button', { name: 'Serif' }));
      expect(screen.getByRole('article').style.fontFamily).toContain('Charter');
      expect(screen.getByRole('navigation', { name: 'Chapters' }).style.fontFamily).toBe('');
    });

    it('uses a custom font family with its fallback', async () => {
      mount();
      await openSettings();
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      await userEvent.type(screen.getByLabelText('Font family'), 'Literata');
      await userEvent.click(screen.getByRole('button', { name: 'Save font' }));
      expect(screen.getByRole('article').style.fontFamily).toContain('Literata');
    });

    it('keeps the settings panel within the screen: it scrolls when it is long', async () => {
      mount({ wide: false });
      await openSettings();
      expect(screen.getByRole('group', { name: 'Reading settings' }).parentElement).toHaveClass(
        'overflow-y-auto',
      );
    });
  });

  describe('phone width', () => {
    it('keeps Previous/Next in a sticky bottom bar of at least 44 px buttons', () => {
      mount({ wide: false });
      const bar = screen.getByRole('navigation', { name: 'Chapters' });
      expect(bar).toHaveClass('sticky', 'bottom-0');
      expect(within(bar).getByRole('button', { name: 'Next chapter' })).toHaveClass('min-h-11');
      expect(within(bar).getByRole('button', { name: 'Previous chapter' })).toHaveClass('min-h-11');
    });

    it('lets a short chapter fill the screen so the bar stays at the bottom edge', () => {
      mount({ wide: false });
      expect(screen.getByTestId('reader-surface')).toHaveClass('flex-1');
      expect(screen.getByTestId('reader-underlay')).toHaveClass('flex-1');
      // The page covers the whole area, so only the strip it has moved away from shows the underlay.
      expect(screen.getByTestId('reader-page')).toHaveClass('flex-1', 'bg-page');
      expect(screen.getByRole('article')).toHaveClass('flex-1');
      // The root takes whatever the shell leaves, down to the bottom edge, with no padding below it.
      const root = screen.getByTestId('reader-root');
      expect(root).toHaveClass('flex-1', '-mb-4');
      expect(root.className).not.toContain('calc(');
    });

    it('lets long words break so the page cannot scroll sideways', () => {
      mount({ wide: false });
      expect(screen.getByRole('article')).toHaveClass('[overflow-wrap:anywhere]');
    });
  });

  describe('touch gestures', () => {
    const surface = () => screen.getByTestId('reader-surface');
    const article = () => screen.getByTestId('reader-page');

    function geometry({ y = 400, height = 2000 }: { y?: number; height?: number } = {}) {
      Object.defineProperty(window, 'scrollY', { configurable: true, value: y });
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 800 });
      Object.defineProperty(document.documentElement, 'scrollHeight', {
        configurable: true,
        value: height,
      });
    }

    function finger(
      type: 'touchStart' | 'touchMove' | 'touchEnd' | 'touchCancel',
      x: number,
      y: number,
    ) {
      const point = { clientX: x, clientY: y };
      fireEvent[type](surface(), {
        touches: type === 'touchEnd' || type === 'touchCancel' ? [] : [point],
        changedTouches: [point],
      });
    }

    function swipe(dx: number, dy = 0) {
      finger('touchStart', 500, 300);
      finger('touchMove', 500 + dx / 2, 300 + dy / 2);
      finger('touchMove', 500 + dx, 300 + dy);
      finger('touchEnd', 500 + dx, 300 + dy);
    }

    afterEach(() => {
      Reflect.deleteProperty(window, 'scrollY');
      Reflect.deleteProperty(window, 'innerHeight');
      Reflect.deleteProperty(document.documentElement, 'scrollHeight');
    });

    it('moves the column with the finger while dragging horizontally', () => {
      geometry();
      mount();
      finger('touchStart', 500, 300);
      finger('touchMove', 470, 300);
      finger('touchMove', 440, 302);
      expect(article().style.transform).toBe('translateX(-41.5px)');
    });

    it('a committed swipe left goes to the next chapter, right to the previous one', async () => {
      geometry();
      mount();
      swipe(-300);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      });
      swipe(300);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
      });
    });

    it('an uncommitted swipe springs back and leaves the URL unchanged', async () => {
      geometry();
      mount();
      finger('touchStart', 500, 300);
      finger('touchMove', 480, 300);
      finger('touchMove', 460, 300);
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 600));
      });
      finger('touchEnd', 460, 300);
      expect(article().style.transform).toBe('translateX(0px)');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
    });

    describe('what the swipe uncovers', () => {
      const hint = (side: 'previous' | 'next') => screen.getByTestId(`hint-${side}`);
      const bubble = (side: 'previous' | 'next') => screen.getByTestId(`bubble-${side}`);
      const tint = () => screen.getByTestId('reader-tint');
      const drag = (...xs: number[]) => {
        finger('touchStart', 500, 300);
        for (const x of xs) finger('touchMove', x, 300);
      };

      it('shows nothing at rest: no tint, no bubble, no edge on the page', () => {
        geometry();
        mount();
        expect(tint().style.opacity).toBe('0');
        expect(hint('previous').style.opacity).toBe('0');
        expect(hint('next').style.opacity).toBe('0');
        expect(hint('next').style.transform).not.toBe('translateX(0px)'); // parked off screen
        expect(article().className).not.toContain('color-mix');
      });

      it('dragging right uncovers a "Previous chapter" bubble beside the page', () => {
        geometry();
        mount();
        drag(560, 620);
        expect(bubble('previous')).toHaveTextContent('Previous chapter');
        expect(bubble('previous')).toHaveAttribute('data-state', 'idle');
        expect(Number(hint('previous').style.opacity)).toBeGreaterThan(0);
        expect(hint('previous').style.transform).toBe('translateX(0px)'); // fully in
        expect(hint('next').style.opacity).toBe('0');
        expect(article().className).toContain('color-mix'); // the page casts a faint shadow
      });

      it('dragging left uncovers a "Next chapter" bubble', () => {
        geometry();
        mount();
        drag(440, 380);
        expect(bubble('next')).toHaveTextContent('Next chapter');
        expect(hint('previous').style.opacity).toBe('0');
      });

      it('makes the strip under the page more contrasting the further the drag goes', () => {
        geometry();
        mount();
        expect(tint().className).toContain('color-mix');
        finger('touchStart', 500, 300);
        finger('touchMove', 460, 300);
        finger('touchMove', 440, 300);
        const early = Number(tint().style.opacity);
        finger('touchMove', 380, 300);
        finger('touchMove', 320, 300);
        const later = Number(tint().style.opacity);
        finger('touchMove', 250, 300);
        const ready = Number(tint().style.opacity);
        expect(early).toBeGreaterThan(0.3); // visible from the first pixels
        expect(later).toBeGreaterThan(early);
        expect(ready).toBeGreaterThan(later);
        expect(ready).toBe(1);
      });

      it('fills the bubble once a release would turn the chapter', () => {
        geometry();
        mount();
        drag(400, 300);
        expect(bubble('next')).toHaveAttribute('data-state', 'idle');
        finger('touchMove', 240, 300);
        expect(bubble('next')).toHaveAttribute('data-state', 'ready');
      });

      it('warns at the end of the book: a warning-coloured bubble that says so', () => {
        geometry();
        mount({ position: 5 });
        drag(440, 380);
        expect(bubble('next')).toHaveAttribute('data-state', 'end');
        expect(bubble('next').className).toContain('bg-gold');
        expect(screen.getByText('Last chapter')).toHaveAttribute('role', 'status');
        expect(bubble('next').querySelector('[data-icon="hand"]')).not.toBeNull();
      });

      it('warns at the start of the book too', () => {
        geometry();
        mount({ position: 1 });
        drag(560, 620);
        expect(bubble('previous')).toHaveAttribute('data-state', 'end');
        expect(screen.getByText('First chapter')).toHaveAttribute('role', 'status');
      });

      it('is fixed to the screen, so it stays put however far down the page is scrolled', () => {
        geometry({ y: 1200, height: 3000 });
        mount();
        drag(440, 380);
        expect(hint('next')).toHaveClass('fixed');
        expect(hint('next').className).not.toContain('sticky');
        expect(hint('next').parentElement?.className ?? '').not.toContain('overflow-hidden');
      });

      it('slides in from the screen edge as the page moves away', () => {
        geometry();
        mount();
        finger('touchStart', 500, 300);
        finger('touchMove', 490, 300);
        finger('touchMove', 480, 300);
        const early = hint('next').style.transform;
        finger('touchMove', 420, 300);
        finger('touchMove', 350, 300);
        expect(early).toMatch(/^translateX\(\d/);
        expect(hint('next').style.transform).toBe('translateX(0px)');
      });

      it('goes away again when the finger lifts', () => {
        geometry();
        mount();
        finger('touchStart', 500, 300);
        finger('touchMove', 480, 300);
        finger('touchMove', 450, 300);
        finger('touchEnd', 450, 300);
        expect(hint('next').style.opacity).toBe('0');
        expect(tint().style.opacity).toBe('0');
      });
    });

    it('a swipe on the first/last chapter shows the hint and does not navigate', () => {
      geometry();
      mount({ position: 1 });
      finger('touchStart', 500, 300);
      finger('touchMove', 560, 300);
      finger('touchMove', 620, 300);
      expect(screen.getByText('First chapter')).toHaveAttribute('role', 'status');
      finger('touchEnd', 620, 300);
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(1)}`);
    });

    it('works on a page opened directly, where the chapter list loads after the first render', async () => {
      geometry();
      mount({ preload: false });
      expect(screen.queryByTestId('reader-surface')).toBeNull();
      await screen.findByRole('article');
      swipe(-300);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      });
    });

    it('a pull at the end of the text works on a directly opened page too', async () => {
      geometry({ y: 1200, height: 2000 });
      mount({ preload: false });
      await screen.findByRole('article');
      finger('touchStart', 500, 600);
      finger('touchMove', 500, 560);
      finger('touchMove', 500, 440);
      finger('touchEnd', 500, 440);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      });
    });

    it('the whole zone under the app header moves: the top row slides with the text', () => {
      geometry();
      mount();
      const page = screen.getByTestId('reader-page');
      expect(within(page).getByRole('link', { name: 'Back to novel' })).toBeInTheDocument();
      expect(within(page).getByRole('button', { name: 'Reading settings' })).toBeInTheDocument();
      expect(within(page).getByRole('article')).toBeInTheDocument();
    });

    it('a swipe may start on the top row', () => {
      geometry();
      mount();
      const back = screen.getByRole('link', { name: 'Back to novel' });
      const touch = (x: number) => ({ touches: [{ clientX: x, clientY: 100 }] });
      fireEvent.touchStart(back, touch(500));
      fireEvent.touchMove(back, touch(440));
      fireEvent.touchMove(back, touch(380));
      expect(article().style.transform).not.toBe('translateX(0px)');
    });

    it('the strip runs edge to edge from the app header to the bottom bar', () => {
      geometry();
      mount();
      expect(screen.getByTestId('reader-root')).toHaveClass('-mx-4', '-mt-4');
    });

    it('a swipe that starts at the screen edge is ignored', () => {
      geometry();
      mount();
      finger('touchStart', 5, 300);
      finger('touchMove', 100, 300);
      finger('touchMove', 300, 300);
      expect(article().style.transform).toBe('translateX(0px)');
    });

    it('a pull past the end of the text shows the indicator, flips to Release, and navigates on release', async () => {
      geometry({ y: 1200, height: 2000 });
      mount();
      finger('touchStart', 500, 600);
      finger('touchMove', 500, 560);
      finger('touchMove', 500, 540);
      expect(screen.getByText('Pull for next chapter')).toBeInTheDocument();
      finger('touchMove', 500, 440);
      expect(screen.getByText('Release for next chapter')).toBeInTheDocument();
      finger('touchEnd', 500, 440);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      });
    });

    it('a pull past the start of the text goes to the previous chapter', async () => {
      geometry({ y: 0, height: 2000 });
      mount();
      finger('touchStart', 500, 300);
      finger('touchMove', 500, 340);
      finger('touchMove', 500, 440);
      finger('touchEnd', 500, 440);
      await waitFor(() => {
        expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(2)}`);
      });
    });

    it('a pull on the last chapter says so and does not navigate', () => {
      geometry({ y: 1200, height: 2000 });
      mount({ position: 5 });
      finger('touchStart', 500, 600);
      finger('touchMove', 500, 560);
      finger('touchMove', 500, 400);
      expect(screen.getByText('Last chapter')).toBeInTheDocument();
      finger('touchEnd', 500, 400);
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(5)}`);
    });

    it('a mouse drag does nothing', () => {
      geometry();
      mount();
      fireEvent.mouseDown(surface(), { clientX: 500, clientY: 300 });
      fireEvent.mouseMove(surface(), { clientX: 200, clientY: 300 });
      fireEvent.mouseUp(surface(), { clientX: 200, clientY: 300 });
      expect(article().style.transform).toBe('translateX(0px)');
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(3)}`);
    });

    it('with reduced motion there is no slide and the swipe navigates at once', () => {
      geometry();
      mount({ reducedMotion: true });
      swipe(-300);
      expect(where()).toBe(`/novels/${NOVEL}/chapters/${idOf(4)}`);
      expect(article().style.transition).toBe('none');
    });

    it('a second finger cancels the swipe', () => {
      geometry();
      mount();
      finger('touchStart', 500, 300);
      finger('touchMove', 400, 300);
      fireEvent.touchStart(surface(), {
        touches: [
          { clientX: 400, clientY: 300 },
          { clientX: 100, clientY: 300 },
        ],
      });
      expect(article().style.transform).toBe('translateX(0px)');
    });

    it('stops pull-to-refresh and chaining while the reader is open, and restores it after', () => {
      const { view } = mount();
      expect(document.documentElement).toHaveClass('reader-open');
      view.unmount();
      expect(document.documentElement).not.toHaveClass('reader-open');
    });
  });

  describe('Back to novel', () => {
    it('links to /novels/:id', () => {
      mount();
      expect(screen.getByRole('link', { name: 'Back to novel' })).toHaveAttribute(
        'href',
        `/novels/${NOVEL}`,
      );
    });
  });
});
