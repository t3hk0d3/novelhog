import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { FollowToggle } from './FollowToggle';

describe('FollowToggle', () => {
  it('when followed shows a gold filled star and "Following"', () => {
    const { container } = render(<FollowToggle followed onChange={vi.fn()} />);
    const button = screen.getByRole('button', { name: 'Following' });
    expect(button).toHaveAttribute('aria-pressed', 'true');
    const star = container.querySelector('svg');
    expect(star).toHaveAttribute('fill', 'currentColor');
    expect(button.className).toContain('text-gold');
  });

  it('when not followed shows a grey outline star and "Follow"', () => {
    const { container } = render(<FollowToggle followed={false} onChange={vi.fn()} />);
    const button = screen.getByRole('button', { name: 'Follow' });
    expect(button).toHaveAttribute('aria-pressed', 'false');
    expect(container.querySelector('svg')).toHaveAttribute('fill', 'none');
    expect(button.className).toContain('text-ink-muted');
  });

  it('on click reports the opposite value', async () => {
    const onChange = vi.fn();
    render(<FollowToggle followed onChange={onChange} />);
    await userEvent.click(screen.getByRole('button'));
    expect(onChange).toHaveBeenCalledExactlyOnceWith(false);
  });

  it('when disabled does not report clicks', async () => {
    const onChange = vi.fn();
    render(<FollowToggle followed={false} disabled onChange={onChange} />);
    await userEvent.click(screen.getByRole('button'));
    expect(onChange).not.toHaveBeenCalled();
  });
});
