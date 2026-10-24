import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ChapterState } from '../api/client';
import { StatusTab } from './StatusTab';

const states: ChapterState[] = ['not_requested', 'pending', 'downloading', 'downloaded', 'error'];

describe('StatusTab', () => {
  describe('in normal mode', () => {
    it('has a chevron leading edge', () => {
      render(<StatusTab state="pending" />);
      expect(screen.getByTestId('status-tab')).toHaveAttribute('data-chevron', 'true');
      expect(screen.getByTestId('status-tab').className).toContain('clip-path');
    });
  });

  describe('in select mode', () => {
    it('is a plain rectangle', () => {
      render(<StatusTab state="pending" chevron={false} />);
      expect(screen.getByTestId('status-tab')).toHaveAttribute('data-chevron', 'false');
      expect(screen.getByTestId('status-tab').className).not.toContain('clip-path');
    });
  });

  describe.each(states)('for %s', (state) => {
    it('uses the state colour and the status icon', () => {
      const { container } = render(<StatusTab state={state} />);
      const tab = screen.getByTestId('status-tab');
      expect(tab.style.backgroundColor).toBe(`var(--status-${state.replace('_', '-')}-tab)`);
      expect(container.querySelector(`svg[data-state="${state}"]`)).not.toBeNull();
    });
  });

  it('labels the icon for screen readers', () => {
    render(<StatusTab state="error" />);
    expect(screen.getByText('Error')).toHaveClass('sr-only');
  });
});
