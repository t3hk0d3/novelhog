import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { StateCounts } from '../hooks/useChapterView';
import { ChapterFilters } from './ChapterFilters';

const counts: StateCounts = {
  not_requested: 480,
  pending: 25,
  downloading: 0,
  downloaded: 152,
  error: 2,
};

function filters(overrides: Partial<Parameters<typeof ChapterFilters>[0]> = {}) {
  const props = {
    query: '',
    onQuery: vi.fn(),
    states: new Set<never>(),
    onToggleState: vi.fn(),
    newOnly: false,
    onToggleNew: vi.fn(),
    newCount: 3,
    counts,
    ...overrides,
  };
  render(<ChapterFilters {...props} />);
  return props;
}

describe('ChapterFilters', () => {
  it('reports what is typed in the search field', async () => {
    const props = filters();
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search number or title' }), '12');
    expect(props.onQuery).toHaveBeenLastCalledWith('2');
    expect(props.onQuery).toHaveBeenCalledTimes(2);
  });

  it('shows the search text it was given', () => {
    filters({ query: 'storm' });
    expect(screen.getByRole('searchbox')).toHaveValue('storm');
  });

  it('shows a chip per state with its status icon and count', () => {
    filters();
    const chip = screen.getByRole('button', { name: /^Error\s*2$/ });
    expect(chip.querySelector('[data-state="error"]')).not.toBeNull();
    expect(screen.getByRole('button', { name: /^Not requested\s*480$/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^Downloaded\s*152$/ })).toBeInTheDocument();
  });

  it('toggles a state filter and marks the active ones pressed', async () => {
    const props = filters({ states: new Set(['error' as const]) });
    expect(screen.getByRole('button', { name: /^Error/ })).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: /^Pending/ }));
    expect(props.onToggleState).toHaveBeenCalledWith('pending');
  });

  it('offers "New" with the number of chapters released since the last visit', async () => {
    const props = filters({ newCount: 3 });
    await userEvent.click(screen.getByRole('button', { name: 'New 3' }));
    expect(props.onToggleNew).toHaveBeenCalled();
  });
});
