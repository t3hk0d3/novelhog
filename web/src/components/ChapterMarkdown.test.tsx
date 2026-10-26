import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ChapterMarkdown } from './ChapterMarkdown';

const PNG = 'data:image/png;base64,iVBORw0KGgo=';

describe('ChapterMarkdown', () => {
  describe('paragraphs, emphasis and lists', () => {
    it('renders them as paragraph, em/strong and list elements', () => {
      const { container } = render(
        <ChapterMarkdown
          markdown={'One *two* **three**\n\nNext paragraph\n\n- a\n- b\n\n1. x\n2. y'}
        />,
      );
      expect(container.querySelectorAll('p')).toHaveLength(2);
      expect(container.querySelector('em')).toHaveTextContent('two');
      expect(container.querySelector('strong')).toHaveTextContent('three');
      expect(container.querySelectorAll('ul > li')).toHaveLength(2);
      expect(container.querySelectorAll('ol > li')).toHaveLength(2);
    });
  });

  describe('image with a data:image/png URI', () => {
    it('is not loaded lazily: the data is already here, and lazy images would keep their height unknown', () => {
      render(<ChapterMarkdown markdown={`![a map](${PNG})`} />);
      expect(screen.getByRole('img', { name: 'a map' })).not.toHaveAttribute('loading', 'lazy');
    });

    it('renders an img with that src', () => {
      render(<ChapterMarkdown markdown={`![a map](${PNG})`} />);
      expect(screen.getByRole('img', { name: 'a map' })).toHaveAttribute('src', PNG);
    });
  });

  describe('image with a multi-megabyte data URI', () => {
    it('renders it with its src intact', () => {
      const big = `data:image/png;base64,${'A'.repeat(3_000_000)}`;
      render(<ChapterMarkdown markdown={`![big](${big})`} />);
      expect(screen.getByRole('img', { name: 'big' })).toHaveAttribute('src', big);
    });
  });

  describe('image with a data URI of another type or a http(s)/javascript URL', () => {
    it.each([
      ['svg data', 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='],
      ['html data', 'data:text/html;base64,PHNjcmlwdD4='],
      ['http', 'http://example.com/a.png'],
      ['https', 'https://example.com/a.png'],
      ['javascript', 'javascript:alert(1)'],
    ])('does not render the src of %s', (_name, url) => {
      const { container } = render(<ChapterMarkdown markdown={`![alt text](${url})`} />);
      expect(container.querySelector('img[src]')).toBeNull();
      expect(screen.getByText('alt text')).toBeInTheDocument();
    });
  });

  describe('raw HTML in the text', () => {
    it('is not rendered as markup', () => {
      const { container } = render(
        <ChapterMarkdown markdown={'<script>alert(1)</script>\n\nHello <b>bold</b>'} />,
      );
      expect(container.querySelector('script')).toBeNull();
      expect(container.querySelector('b')).toBeNull();
    });
  });

  describe('"[image unavailable]" text', () => {
    it('is shown as plain text', () => {
      render(<ChapterMarkdown markdown={'Before\n\n\\[image unavailable\\]\n\nAfter'} />);
      expect(screen.getByText('[image unavailable]')).toBeInTheDocument();
    });
  });
});
