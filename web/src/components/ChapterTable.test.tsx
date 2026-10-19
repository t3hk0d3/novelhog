import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { act, fireEvent } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useSelection } from '../hooks/useSelection';
import { makeChapter, makeChapters } from '../test/fixtures';
import { renderWithProviders as render } from '../test/render';
import { ChapterTable } from './ChapterTable';

function setWide(wide: boolean, reducedMotion = false) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: query.includes('reduced-motion') ? reducedMotion : wide,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const never = () => false;

describe('ChapterTable', () => {
  describe('on desktop', () => {
    it('shows the column headings', () => {
      setWide(true);
      render(<ChapterTable chapters={[makeChapter(1)]} isNew={never} onOpenDetails={vi.fn()} />);
      for (const heading of ['#', 'Title', 'Added', 'Updated']) {
        expect(screen.getByText(heading)).toBeInTheDocument();
      }
    });

    it('shows the release date only and a relative update time', () => {
      setWide(true);
      const chapter = makeChapter(1, {
        released_at: '2026-10-09T12:00:00.000Z',
        updated_at: new Date(Date.now() - 12 * 60_000).toISOString(),
      });
      render(<ChapterTable chapters={[chapter]} isNew={never} onOpenDetails={vi.fn()} />);
      expect(screen.getByText(/^2026-10-(09|10)$/)).toBeInTheDocument();
      expect(screen.getByText('12 min ago')).toBeInTheDocument();
    });

    it('shows a dash with a tooltip for the initial import', () => {
      setWide(true);
      const chapter = makeChapter(1, { released_at: null });
      render(<ChapterTable chapters={[chapter]} isNew={never} onOpenDetails={vi.fn()} />);
      expect(screen.getByText('—')).toHaveAttribute('title', 'Part of the initial import');
    });
  });

  describe('on a phone', () => {
    it('shows position and title but no Added or Updated', () => {
      setWide(false);
      render(<ChapterTable chapters={[makeChapter(7)]} isNew={never} onOpenDetails={vi.fn()} />);
      expect(screen.getByText('7')).toBeInTheDocument();
      expect(screen.getByText('Page 7')).toBeInTheDocument();
      expect(screen.queryByText('Added')).toBeNull();
      expect(screen.queryByText('Updated')).toBeNull();
      expect(screen.queryByText('just now')).toBeNull();
    });
  });

  describe('rows', () => {
    it('are links named "<title>, <status>, open details"', () => {
      render(
        <ChapterTable
          chapters={[makeChapter(1, { state: 'pending' })]}
          isNew={never}
          onOpenDetails={vi.fn()}
        />,
      );
      expect(
        screen.getByRole('link', { name: 'Page 1, Pending, open details' }),
      ).toBeInTheDocument();
    });

    it('link to the chapter inside the app, not to the source site', () => {
      const chapter = makeChapter(4);
      render(<ChapterTable chapters={[chapter]} isNew={never} onOpenDetails={vi.fn()} />, {
        route: '/novels/NV000000000001',
      });
      const href = screen.getByRole('link').getAttribute('href');
      expect(href).toBe(`/novels/NV000000000001?chapter=${chapter.id}`);
      expect(href).not.toContain(chapter.url);
    });

    it('show the new dot only for new chapters', () => {
      const chapters = [makeChapter(1), makeChapter(2)];
      render(
        <ChapterTable
          chapters={chapters}
          isNew={(c) => c.position === 2}
          onOpenDetails={vi.fn()}
        />,
      );
      const [first, second] = screen.getAllByRole('link');
      expect(within(first as HTMLElement).queryByTestId('new-dot')).toBeNull();
      expect(within(second as HTMLElement).getByTestId('new-dot')).toBeInTheDocument();
    });

    it('never show error text', () => {
      const chapter = makeChapter(1, {
        state: 'error',
        error: 'HTTP 503 from upstream',
        error_at: '2026-10-19T10:00:00.000Z',
      });
      const { container } = render(
        <ChapterTable chapters={[chapter]} isNew={never} onOpenDetails={vi.fn()} />,
      );
      expect(container).not.toHaveTextContent('HTTP 503');
    });

    it('call onOpenDetails with the chapter when clicked', async () => {
      const onOpenDetails = vi.fn();
      const chapter = makeChapter(3);
      render(<ChapterTable chapters={[chapter]} isNew={never} onOpenDetails={onOpenDetails} />);
      await userEvent.click(screen.getByRole('link'));
      expect(onOpenDetails).toHaveBeenCalledExactlyOnceWith(chapter);
    });
  });

  describe('select mode', () => {
    const chapters = makeChapters(6);

    function Harness({ onOpenDetails = vi.fn() }: { onOpenDetails?: () => void }) {
      const selection = useSelection(chapters, chapters);
      return (
        <>
          <button
            onClick={() => {
              selection.enter();
            }}
          >
            Select
          </button>
          <button onClick={selection.selectAll}>All</button>
          <output aria-label="selected">
            {selection.selected.map((c) => c.position).join(',')}
          </output>
          <p role="status">{selection.announcement}</p>
          <ChapterTable
            chapters={chapters}
            isNew={never}
            onOpenDetails={onOpenDetails}
            selection={selection}
          />
        </>
      );
    }
    const selectedOutput = () => screen.getByLabelText('selected').textContent;
    const option = (n: number) =>
      screen.getByRole('option', { name: new RegExp(`^Page ${String(n)},`) });
    const row = (n: number) =>
      screen.getByRole('link', { name: new RegExp(`^Page ${String(n)},`) });

    describe('entering from normal mode', () => {
      it('Shift-click enters with that row selected and does not open details', () => {
        const onOpenDetails = vi.fn();
        render(<Harness onOpenDetails={onOpenDetails} />);
        fireEvent.click(row(3), { shiftKey: true });
        expect(selectedOutput()).toBe('3');
        expect(onOpenDetails).not.toHaveBeenCalled();
        expect(screen.getByRole('listbox')).toBeInTheDocument();
      });

      it.each([['Control'], ['Meta']])('%s-click enters with that row selected', (key) => {
        render(<Harness />);
        fireEvent.click(row(2), { ctrlKey: key === 'Control', metaKey: key === 'Meta' });
        expect(selectedOutput()).toBe('2');
      });

      it('a long press enters with that row selected and swallows the click', () => {
        vi.useFakeTimers();
        const onOpenDetails = vi.fn();
        render(<Harness onOpenDetails={onOpenDetails} />);
        const target = row(4);
        fireEvent.pointerDown(target, { button: 0 });
        act(() => {
          vi.advanceTimersByTime(600);
        });
        fireEvent.pointerUp(target);
        fireEvent.click(option(4));
        expect(selectedOutput()).toBe('4');
        expect(onOpenDetails).not.toHaveBeenCalled();
        vi.useRealTimers();
      });

      it('suppresses the browser context menu while a press is pending', () => {
        vi.useFakeTimers();
        render(<Harness />);
        const target = row(4);
        fireEvent.pointerDown(target, { button: 0 });
        // The menu arriving before our timer (Android) is cancelled and counts as the long press.
        const notCancelled = fireEvent.contextMenu(target);
        expect(notCancelled).toBe(false);
        expect(selectedOutput()).toBe('4');
        vi.useRealTimers();
      });

      it('leaves the context menu alone when no press is pending', () => {
        render(<Harness />);
        expect(fireEvent.contextMenu(row(4))).toBe(true);
      });

      it('a short press does not enter select mode', () => {
        vi.useFakeTimers();
        render(<Harness />);
        fireEvent.pointerDown(row(4), { button: 0 });
        act(() => {
          vi.advanceTimersByTime(200);
        });
        fireEvent.pointerUp(row(4));
        act(() => {
          vi.advanceTimersByTime(600);
        });
        expect(screen.queryByRole('listbox')).toBeNull();
        vi.useRealTimers();
      });

      it('moving the pointer away cancels the long press', () => {
        vi.useFakeTimers();
        render(<Harness />);
        fireEvent.pointerDown(row(4), { button: 0, clientX: 0, clientY: 0 });
        fireEvent.pointerMove(row(4), { clientX: 0, clientY: 40 });
        act(() => {
          vi.advanceTimersByTime(600);
        });
        expect(screen.queryByRole('listbox')).toBeNull();
        vi.useRealTimers();
      });

      it('a plain click still opens details', async () => {
        const onOpenDetails = vi.fn();
        render(<Harness onOpenDetails={onOpenDetails} />);
        await userEvent.click(row(1));
        expect(onOpenDetails).toHaveBeenCalledOnce();
        expect(screen.queryByRole('listbox')).toBeNull();
      });
    });

    describe('checkboxes', () => {
      it('appear at the start of every row, hidden from assistive tech', async () => {
        render(<Harness />);
        expect(screen.queryAllByTestId('row-checkbox')).toHaveLength(0);
        await userEvent.click(screen.getByText('Select'));
        const boxes = screen.getAllByTestId('row-checkbox');
        expect(boxes).toHaveLength(6);
        expect(boxes[0]).toHaveAttribute('aria-hidden', 'true');
        expect(boxes[0]?.className).toContain('transition');
      });

      it('have no transition class with prefers-reduced-motion', async () => {
        setWide(false, true);
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        for (const box of screen.getAllByTestId('row-checkbox')) {
          expect(box.className).not.toContain('transition');
          expect(box.className).not.toContain('starting');
        }
      });

      it('status tabs lose their chevron', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        for (const tab of screen.getAllByTestId('status-tab'))
          expect(tab).toHaveAttribute('data-chevron', 'false');
      });
    });

    describe('selecting', () => {
      it('a click toggles a row and never opens details', async () => {
        const onOpenDetails = vi.fn();
        render(<Harness onOpenDetails={onOpenDetails} />);
        await userEvent.click(screen.getByText('Select'));
        await userEvent.click(option(2));
        await userEvent.click(option(5));
        expect(selectedOutput()).toBe('2,5');
        await userEvent.click(option(2));
        expect(selectedOutput()).toBe('5');
        expect(onOpenDetails).not.toHaveBeenCalled();
      });

      it('Space toggles the focused row', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        option(3).focus();
        await userEvent.keyboard(' ');
        expect(selectedOutput()).toBe('3');
      });

      it('Shift-click ranges from the last selected row, not from a deselected one', () => {
        render(<Harness />);
        fireEvent.click(screen.getByText('Select'));
        fireEvent.click(option(2));
        fireEvent.click(option(4));
        fireEvent.click(option(4));
        fireEvent.click(option(6), { shiftKey: true });
        expect(selectedOutput()).toBe('2,3,4,5,6');
      });

      it('Shift-click selects from the last selected row', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        await userEvent.click(option(2));
        fireEvent.click(option(5), { shiftKey: true });
        expect(selectedOutput()).toBe('2,3,4,5');
      });

      it('Ctrl-click toggles just that row', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        await userEvent.click(option(2));
        fireEvent.click(option(5), { ctrlKey: true });
        expect(selectedOutput()).toBe('2,5');
      });

      it('a long press selects from the last selected row', () => {
        vi.useFakeTimers();
        render(<Harness />);
        fireEvent.click(screen.getByText('Select'));
        fireEvent.click(option(1));
        fireEvent.pointerDown(option(4), { button: 0 });
        act(() => {
          vi.advanceTimersByTime(600);
        });
        fireEvent.pointerUp(option(4));
        fireEvent.click(option(4));
        expect(selectedOutput()).toBe('1,2,3,4');
        vi.useRealTimers();
      });
    });

    describe('accessibility', () => {
      it('is a multi-select list with aria-selected per row', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        await userEvent.click(option(2));
        expect(screen.getByRole('listbox', { name: 'Chapters 1–6' })).toHaveAttribute(
          'aria-multiselectable',
          'true',
        );
        expect(option(2)).toHaveAttribute('aria-selected', 'true');
        expect(option(3)).toHaveAttribute('aria-selected', 'false');
      });

      it('announces entering select mode', async () => {
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        expect(screen.getByText('Select mode, 0 selected')).toBeInTheDocument();
      });
    });

    describe('the desktop header checkbox', () => {
      it('is absent in normal mode', () => {
        setWide(true);
        render(<Harness />);
        expect(screen.queryByRole('checkbox')).toBeNull();
      });

      it('is absent on phones, even in select mode', async () => {
        setWide(false);
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        expect(screen.getByRole('listbox')).toBeInTheDocument();
        expect(screen.queryByRole('checkbox')).toBeNull();
      });

      it('shows none, some and all, and toggles every visible row', async () => {
        setWide(true);
        render(<Harness />);
        await userEvent.click(screen.getByText('Select'));
        const box = screen.getByRole('checkbox', { name: 'Select all visible chapters' });
        expect(box).toHaveAttribute('aria-checked', 'false');
        await userEvent.click(option(1));
        expect(box).toHaveAttribute('aria-checked', 'mixed');
        await userEvent.click(box);
        expect(box).toHaveAttribute('aria-checked', 'true');
        expect(selectedOutput()).toBe('1,2,3,4,5,6');
        await userEvent.click(box);
        expect(selectedOutput()).toBe('');
        expect(box).toHaveAttribute('aria-checked', 'false');
      });
    });
  });
});
