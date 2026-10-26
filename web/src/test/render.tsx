import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { vi } from 'vitest';
import { ToastProvider } from '../components/Toast';
import { useLiveEvents } from '../hooks/useLiveEvents';
import { FakeEventSource } from './FakeEventSource';

/** A client that never retries, so failed requests surface immediately in tests. */
export function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function LiveStream({ client }: { client: QueryClient }) {
  useLiveEvents(client);
  return null;
}

/**
 * Renders `ui` with the query client, router and toast providers the app shell supplies. With
 * `live`, the event stream is a FakeEventSource that has already said `ready`, so queries gated
 * on it run (tests unstub the global themselves).
 */
export function renderWithProviders(
  ui: ReactElement,
  {
    client = testClient(),
    route = '/',
    live = false,
  }: { client?: QueryClient; route?: string; live?: boolean } = {},
) {
  if (live) {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  }
  const rendered = render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        {live && <LiveStream client={client} />}
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
  if (live) {
    act(() => {
      FakeEventSource.instances[0]?.emit('ready');
    });
  }
  return rendered;
}
