import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { makeChapter } from '../test/fixtures';
import { ChapterTable } from './ChapterTable';

function setWide(wide: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: wide,
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
});
