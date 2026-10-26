import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Push, PushStatus } from '../hooks/usePush';
import { renderWithProviders } from '../test/render';
import { PushBanner } from './PushBanner';

function fakePush(status: PushStatus): Push {
  return {
    status,
    busy: false,
    subscribe: vi.fn().mockResolvedValue(undefined),
    unsubscribe: vi.fn().mockResolvedValue(undefined),
  };
}

beforeEach(() => {
  localStorage.clear();
});

describe('PushBanner', () => {
  describe('default', () => {
    it('is shown while not subscribed and not dismissed', () => {
      renderWithProviders(<PushBanner push={fakePush('off')} />);
      expect(
        screen.getByText(
          'Get a notification on this device when a followed novel has new chapters.',
        ),
      ).toBeInTheDocument();
    });

    it('Turn on subscribes', async () => {
      const push = fakePush('off');
      renderWithProviders(<PushBanner push={push} />);
      await userEvent.click(screen.getByRole('button', { name: 'Turn on' }));
      expect(push.subscribe).toHaveBeenCalled();
    });

    it('x hides it and stores novelhog.pushBannerDismissed', async () => {
      renderWithProviders(<PushBanner push={fakePush('off')} />);
      await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByRole('region')).not.toBeInTheDocument();
      expect(localStorage.getItem('novelhog.pushBannerDismissed')).not.toBeNull();
    });

    it('stays hidden when it was dismissed before', () => {
      localStorage.setItem('novelhog.pushBannerDismissed', '1');
      renderWithProviders(<PushBanner push={fakePush('off')} />);
      expect(screen.queryByRole('region')).not.toBeInTheDocument();
    });

    it.each(['on', 'checking', 'hidden', 'needs-https', 'unsupported'] as const)(
      'is not shown when %s',
      (status) => {
        renderWithProviders(<PushBanner push={fakePush(status)} />);
        expect(screen.queryByRole('region')).not.toBeInTheDocument();
      },
    );
  });

  describe('blocked', () => {
    it('is amber with no Turn on, and dismissal is stored separately', async () => {
      renderWithProviders(<PushBanner push={fakePush('blocked')} />);
      const banner = screen.getByRole('region');
      expect(banner).toHaveTextContent('Notifications are blocked for this site.');
      expect(banner).toHaveClass('bg-gold/15');
      expect(screen.queryByRole('button', { name: 'Turn on' })).not.toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
      expect(localStorage.getItem('novelhog.pushBlockedBannerDismissed')).not.toBeNull();
      expect(localStorage.getItem('novelhog.pushBannerDismissed')).toBeNull();
    });

    it('is not hidden by the default banner dismissal', () => {
      localStorage.setItem('novelhog.pushBannerDismissed', '1');
      renderWithProviders(<PushBanner push={fakePush('blocked')} />);
      expect(screen.getByRole('region')).toBeInTheDocument();
    });
  });

  describe('iOS not installed', () => {
    it('explains Add to Home Screen, without Turn on', () => {
      renderWithProviders(<PushBanner push={fakePush('install-first')} />);
      expect(screen.getByRole('region')).toHaveTextContent('Add to Home Screen');
      expect(screen.queryByRole('button', { name: 'Turn on' })).not.toBeInTheDocument();
    });
  });

  describe('storage errors', () => {
    it('show the banner and still let it be dismissed for this page view', async () => {
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new DOMException('denied', 'SecurityError');
      });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new DOMException('denied', 'SecurityError');
      });
      renderWithProviders(<PushBanner push={fakePush('off')} />);
      expect(screen.getByRole('region')).toBeInTheDocument();
      await userEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByRole('region')).not.toBeInTheDocument();
    });
  });
});
