import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { makeChapter } from '../test/fixtures';
import { renderWithProviders } from '../test/render';
import { ChapterPreview } from './ChapterPreview';

const NOVEL = 'NV000000000001';

beforeEach(() => {
  vi.stubGlobal(
    'matchMedia',
    vi.fn((query: string) => ({
      matches: query.includes('min-width'),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function stubContent(failWith?: string) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        failWith === undefined
          ? Response.json({ markdown: 'Story text' })
          : Response.json({ error: { code: 'not_found', message: failWith } }, { status: 404 }),
      ),
    ),
  );
}

function show(onClose = vi.fn()) {
  const chapter = makeChapter(2, { title: 'The Gate' });
  renderWithProviders(<ChapterPreview novelId={NOVEL} chapter={chapter} onClose={onClose} />, {
    live: true,
  });
  return { chapter, onClose };
}

describe('ChapterPreview', () => {
  describe('downloaded chapter', () => {
    it('is a dialog named by the chapter title', () => {
      stubContent();
      show();
      expect(screen.getByRole('dialog', { name: 'The Gate' })).toBeInTheDocument();
    });

    it('shows the full content in a scrollable region', async () => {
      stubContent();
      show();
      const region = screen.getByRole('region', { name: 'Chapter content' });
      await waitFor(() => {
        expect(region).toHaveTextContent('Story text');
      });
      expect(region).toHaveAttribute('tabindex', '0');
      expect(region).toHaveClass('overflow-y-auto');
    });

    it('lets a long unbroken word wrap so the box cannot scroll sideways', () => {
      stubContent();
      show();
      expect(screen.getByRole('region', { name: 'Chapter content' })).toHaveClass(
        '[overflow-wrap:anywhere]',
      );
    });

    it('falls back to "Chapter N" when the chapter has no title', () => {
      stubContent();
      renderWithProviders(
        <ChapterPreview
          novelId={NOVEL}
          chapter={makeChapter(7, { title: null })}
          onClose={vi.fn()}
        />,
        { live: true },
      );
      expect(screen.getByRole('dialog', { name: 'Chapter 7' })).toBeInTheDocument();
    });

    it('has no Read link: Back is its only way out besides the ×', () => {
      stubContent();
      show();
      expect(screen.queryByRole('link', { name: 'Read' })).toBeNull();
    });
  });

  describe('content loading', () => {
    it('shows a loading line, not an empty box', () => {
      stubContent();
      show();
      expect(screen.getByText('Loading content…')).toBeInTheDocument();
    });
  });

  describe('content request fails', () => {
    it('shows the error message in the region', async () => {
      stubContent('Content is gone');
      show();
      await waitFor(() => {
        expect(screen.getByRole('region', { name: 'Chapter content' })).toHaveTextContent(
          'Content is gone',
        );
      });
    });
  });

  describe('Back', () => {
    it('calls onClose, returning to the chapter details', async () => {
      stubContent();
      const { onClose } = show();
      await userEvent.click(screen.getByRole('button', { name: 'Back' }));
      expect(onClose).toHaveBeenCalledOnce();
    });
  });

  describe('Close', () => {
    it('calls onClose', async () => {
      stubContent();
      const { onClose } = show();
      await userEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(onClose).toHaveBeenCalledOnce();
    });
  });
});
