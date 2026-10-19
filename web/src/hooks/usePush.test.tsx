import { QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { makeSettings } from '../test/fixtures';
import { fakeBrowser } from '../test/push';
import { testClient } from '../test/render';
import { SUBSCRIPTION_ID_KEY, usePush } from './usePush';

vi.mock('./useLiveEvents', () => ({ useLiveReady: () => true }));

let webpush: Parameters<typeof makeSettings>[0];
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  webpush = {};
  fetchMock = vi.fn((input: string, init?: RequestInit) => {
    if (input.endsWith('/settings')) return Promise.resolve(Response.json(makeSettings(webpush)));
    if (init?.method === 'POST') {
      return Promise.resolve(Response.json({ id: 'SB1', channel: 'webpush' }, { status: 201 }));
    }
    return Promise.resolve(new Response(null, { status: 204 }));
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  Reflect.deleteProperty(navigator, 'serviceWorker');
  vi.unstubAllGlobals();
});

function mount() {
  const client = testClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(() => usePush(), { wrapper });
}

const calls = (method: string) =>
  fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === method);

describe('usePush', () => {
  it('is hidden when webpush is disabled in settings', async () => {
    fakeBrowser();
    webpush = { enabled: false, vapid_public_key: null };
    const { result } = mount();
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalled();
    });
    expect(result.current.status).toBe('hidden');
  });

  it('needs HTTPS outside a secure context', async () => {
    fakeBrowser({ secure: false });
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('needs-https');
    });
  });

  it('asks to install first on iOS outside an installed PWA', async () => {
    fakeBrowser();
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)',
    );
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('install-first');
    });
  });

  it('is not install-first on iOS in an installed PWA (navigator.standalone)', async () => {
    fakeBrowser();
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)',
    );
    Object.defineProperty(navigator, 'standalone', { configurable: true, value: true });
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('off');
    });
    Reflect.deleteProperty(navigator, 'standalone');
  });

  it('is not install-first on iOS in display-mode standalone', async () => {
    fakeBrowser();
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (iPad; CPU OS 17_0)');
    vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('standalone') }));
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('off');
    });
  });

  it('is blocked when Notification.permission is denied', async () => {
    fakeBrowser({ permission: 'denied' });
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('blocked');
    });
  });

  it('is unsupported when the browser has no push', async () => {
    fakeBrowser();
    Reflect.deleteProperty(navigator, 'serviceWorker');
    const { result } = mount();
    await waitFor(() => {
      expect(result.current.status).toBe('unsupported');
    });
  });

  it('reports on when getSubscription returns one, off when not', async () => {
    fakeBrowser({ subscribed: true });
    const on = mount();
    await waitFor(() => {
      expect(on.result.current.status).toBe('on');
    });
    fakeBrowser({ subscribed: false });
    const off = mount();
    await waitFor(() => {
      expect(off.result.current.status).toBe('off');
    });
  });

  describe('subscribe', () => {
    it('requests permission, subscribes with the key, POSTs and stores the id', async () => {
      const browser = fakeBrowser();
      const { result } = mount();
      await waitFor(() => {
        expect(result.current.status).toBe('off');
      });
      await act(() => result.current.subscribe());
      expect(browser.requestPermission).toHaveBeenCalled();
      const options = browser.pushManager.subscribe.mock.calls[0]?.[0] as {
        userVisibleOnly: boolean;
        applicationServerKey: Uint8Array;
      };
      expect(options.userVisibleOnly).toBe(true);
      expect([...options.applicationServerKey]).toEqual([...new Uint8Array([4, 5, 3])]);
      const [url, init] = calls('POST')[0] as [string, RequestInit];
      expect(url).toBe('/api/v1/webpush/subscriptions');
      expect(JSON.parse(init.body as string)).toEqual({
        endpoint: 'https://fcm.googleapis.com/send/abc',
        keys: { p256dh: 'pk', auth: 'ak' },
      });
      expect(localStorage.getItem(SUBSCRIPTION_ID_KEY)).toBe('SB1');
      expect(result.current.status).toBe('on');
    });

    it('becomes blocked when permission is denied, without subscribing', async () => {
      const browser = fakeBrowser();
      browser.requestPermission.mockImplementation(() => {
        browser.notification.permission = 'denied';
        return Promise.resolve('denied');
      });
      const { result } = mount();
      await waitFor(() => {
        expect(result.current.status).toBe('off');
      });
      await act(() => result.current.subscribe());
      expect(browser.pushManager.subscribe).not.toHaveBeenCalled();
      expect(result.current.status).toBe('blocked');
    });

    it('on InvalidStateError unsubscribes the old subscription and subscribes again', async () => {
      const browser = fakeBrowser();
      browser.pushManager.subscribe.mockRejectedValueOnce(
        new DOMException('key changed', 'InvalidStateError'),
      );
      browser.pushManager.getSubscription
        .mockResolvedValueOnce(null)
        .mockResolvedValue(browser.subscription);
      const { result } = mount();
      await waitFor(() => {
        expect(result.current.status).toBe('off');
      });
      await act(() => result.current.subscribe());
      expect(browser.subscription.unsubscribe).toHaveBeenCalled();
      expect(browser.pushManager.subscribe).toHaveBeenCalledTimes(2);
      expect(result.current.status).toBe('on');
    });
  });

  describe('unsubscribe', () => {
    it('unsubscribes the browser and DELETEs the stored id', async () => {
      const browser = fakeBrowser({ subscribed: true });
      localStorage.setItem(SUBSCRIPTION_ID_KEY, 'SB9');
      const { result } = mount();
      await waitFor(() => {
        expect(result.current.status).toBe('on');
      });
      await act(() => result.current.unsubscribe());
      expect(browser.subscription.unsubscribe).toHaveBeenCalled();
      expect(calls('DELETE')[0]?.[0]).toBe('/api/v1/subscriptions/SB9');
      expect(localStorage.getItem(SUBSCRIPTION_ID_KEY)).toBeNull();
      expect(result.current.status).toBe('off');
    });

    it('looks the id up by re-posting the endpoint when none is stored', async () => {
      fakeBrowser({ subscribed: true });
      const { result } = mount();
      await waitFor(() => {
        expect(result.current.status).toBe('on');
      });
      await act(() => result.current.unsubscribe());
      expect(calls('POST')).toHaveLength(1);
      expect(calls('DELETE')[0]?.[0]).toBe('/api/v1/subscriptions/SB1');
    });
  });
});
