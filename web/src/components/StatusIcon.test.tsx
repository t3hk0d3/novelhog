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

describe('StatusIcon', () => {
  it.each(cases)('renders the icon and the screen-reader label for %s', (state, label) => {
    const { container } = render(<StatusIcon state={state} />);
    expect(screen.getByText(label)).toHaveClass('sr-only');
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('aria-hidden', 'true');
    expect(svg).toHaveAttribute('data-state', state);
  });
});
