import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { Push, PushStatus } from '../hooks/usePush';
import { renderWithProviders } from '../test/render';
import { NotificationsBell } from './NotificationsBell';

function fakePush(status: PushStatus): Push {
  return {
    status,
    busy: false,
    subscribe: vi.fn().mockResolvedValue(undefined),
    unsubscribe: vi.fn().mockResolvedValue(undefined),
  };
}

describe('NotificationsBell', () => {
  it('renders nothing when hidden', () => {
    renderWithProviders(<NotificationsBell push={fakePush('hidden')} />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('is an outline bell that subscribes when off', async () => {
    const push = fakePush('off');
    renderWithProviders(<NotificationsBell push={push} />);
    const bell = screen.getByRole('button', { name: 'Notifications on this device: off' });
    expect(bell.querySelector('[data-icon="bell"]')).toBeInTheDocument();
    await userEvent.click(bell);
    expect(push.subscribe).toHaveBeenCalled();
  });

  it('is a filled bell that unsubscribes when on', async () => {
    const push = fakePush('on');
    renderWithProviders(<NotificationsBell push={push} />);
    const bell = screen.getByRole('button', { name: 'Notifications on this device: on' });
    expect(bell.querySelector('[data-icon="bell-filled"]')).toBeInTheDocument();
    await userEvent.click(bell);
    expect(push.unsubscribe).toHaveBeenCalled();
  });

  it.each([
    ['needs-https', 'Notifications need HTTPS'],
    ['blocked', 'Blocked in browser settings'],
  ] as const)(
    'is struck through and disabled when %s, with the reason as tooltip',
    (status, tip) => {
      renderWithProviders(<NotificationsBell push={fakePush(status)} />);
      const bell = screen.getByRole('button', { name: /Notifications on this device/ });
      expect(bell).toBeDisabled();
      expect(bell).toHaveAttribute('title', tip);
      expect(bell.querySelector('[data-icon="bell-off"]')).toBeInTheDocument();
    },
  );

  it('shows a toast when toggling fails', async () => {
    const push = fakePush('off');
    vi.mocked(push.subscribe).mockRejectedValue(new Error('Server said no'));
    renderWithProviders(<NotificationsBell push={push} />);
    await userEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('Server said no')).toBeInTheDocument();
  });
});
