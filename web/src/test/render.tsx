import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router';
import { ToastProvider } from '../components/Toast';

/** A client that never retries, so failed requests surface immediately in tests. */
export function testClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

/** Renders `ui` with the query client, router and toast providers the app shell supplies. */
export function renderWithProviders(
  ui: ReactElement,
  { client = testClient(), route = '/' }: { client?: QueryClient; route?: string } = {},
) {
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}
