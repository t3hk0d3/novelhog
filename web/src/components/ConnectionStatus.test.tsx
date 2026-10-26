import { act, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';
import { STALL_MS } from '../hooks/useLiveEvents';
import { FakeEventSource } from '../test/FakeEventSource';
import { testClient } from '../test/render';
import { HOST_NOT_ALLOWED, STREAM_BLOCKED } from './ConnectionStatus';

// The push state (and its /settings request) is not under test here.
vi.mock('../hooks/usePush', () => ({
  usePush: () => ({ status: 'hidden', busy: false, subscribe: vi.fn(), unsubscribe: vi.fn() }),
}));

beforeEach(() => {
  localStorage.clear();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource);
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const ROUTES = ['/', '/novels/NV1'];

function mountApp(route: string, fetchMock: ReturnType<typeof vi.fn>) {
  vi.stubGlobal('fetch', fetchMock);
  render(
    <MemoryRouter initialEntries={[route]}>
      <App queryClient={testClient()} />
    </MemoryRouter>,
  );
}

const settingsOk = () =>
  vi.fn(() => Promise.resolve(Response.json({ vapid_public_key: 'k', push_enabled: true })));
const apiError = (status: number, code: string, message: string) =>
  vi.fn(() => Promise.resolve(Response.json({ error: { code, message } }, { status })));

function failStream() {
  act(() => {
    FakeEventSource.instances[0]?.fail();
  });
}

describe('ConnectionStatus', () => {
  describe('While the stream has not sent ready', () => {
    it.each(ROUTES)('%s shows Connecting… and loads nothing', (route) => {
      const fetchMock = settingsOk();
      mountApp(route, fetchMock);
      expect(screen.getByRole('main')).toHaveTextContent('Connecting…');
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });

  describe('When no ready arrives within the stall timeout', () => {
    it.each(ROUTES)('%s probes the settings and names the cause', async (route) => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      const fetchMock = settingsOk();
      mountApp(route, fetchMock);
      act(() => {
        vi.advanceTimersByTime(STALL_MS);
      });
      expect(await screen.findByRole('alert')).toHaveTextContent(STREAM_BLOCKED);
      expect(fetchMock).toHaveBeenCalledWith('/api/v1/settings', { method: 'GET' });
    });
  });

  describe('When the API answers 403 host_not_allowed', () => {
    it.each(ROUTES)('%s explains NOVELHOG_ALLOWED_HOSTS', async (route) => {
      mountApp(route, apiError(403, 'host_not_allowed', 'Host is not allowed'));
      failStream();
      expect(await screen.findByRole('alert')).toHaveTextContent(HOST_NOT_ALLOWED);
    });
  });

  describe('When the API answers another error', () => {
    it('shows its message', async () => {
      mountApp('/', apiError(502, 'http_error', 'Request failed (502)'));
      failStream();
      expect(await screen.findByRole('alert')).toHaveTextContent('Request failed (502)');
    });
  });

  describe('When ready arrives after a stall', () => {
    it('removes the error and loads the page', async () => {
      const fetchMock = vi.fn((url: string) =>
        Promise.resolve(
          Response.json(url === '/api/v1/novels' ? [] : { vapid_public_key: 'k' }, {
            status: 200,
          }),
        ),
      );
      mountApp('/', fetchMock);
      failStream();
      await screen.findByRole('alert');
      act(() => {
        FakeEventSource.instances[0]?.emit('ready');
      });
      expect(await screen.findByText(/Your library is empty/)).toBeInTheDocument();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      expect(screen.getByRole('main')).not.toHaveTextContent('Connecting…');
    });
  });
});
