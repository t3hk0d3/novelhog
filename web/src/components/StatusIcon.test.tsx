import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ChapterState } from '../api/client';
import { StatusIcon } from './StatusIcon';

const cases: [ChapterState, string][] = [
  ['not_requested', 'Not requested'],
  ['pending', 'Pending'],
  ['downloading', 'Downloading'],
  ['downloaded', 'Downloaded'],
  ['error', 'Error'],
];

function glyph(state: ChapterState): string {
  const { container } = render(<StatusIcon state={state} />);
  return container.querySelector('svg')?.innerHTML ?? '';
}

describe('StatusIcon', () => {
  it.each(cases)('renders the icon and the screen-reader label for %s', (state, label) => {
    const { container } = render(<StatusIcon state={state} />);
    expect(screen.getByText(label)).toHaveClass('sr-only');
    expect(container.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
  });

  it('draws a different glyph for every state', () => {
    const glyphs = cases.map(([state]) => glyph(state));
    expect(new Set(glyphs).size).toBe(5);
  });

  it('draws the shapes the status table asks for', () => {
    expect(glyph('not_requested')).toMatch(/^<circle[^>]*><\/circle>$/); // empty circle
    expect(glyph('pending')).toContain('<circle'); // clock face
    expect(glyph('pending')).toContain('<path'); // clock hands
    expect(glyph('downloaded')).toContain('<circle'); // check in a circle
    expect(glyph('error')).toContain('M12 3.5L22 20H2z'); // warning triangle
    expect(glyph('downloading')).not.toContain('<circle'); // arrow only
  });
});
