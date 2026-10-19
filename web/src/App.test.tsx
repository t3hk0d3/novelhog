import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    'EventSource',
    class {
      addEventListener = vi.fn();
      close = vi.fn();
    },
  );
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

describe('App', () => {
  it.each(['/', '/novels/NV123'])('renders the header with the theme menu on %s', (path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Theme: System' })).toBeInTheDocument();
  });

  it('renders a placeholder per route', () => {
    render(
      <MemoryRouter initialEntries={['/novels/NV123']}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByRole('main')).toHaveTextContent('NV123');
  });
});

describe('App header', () => {
  it('Add novel opens the add-novel dialog', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    const banner = screen.getByRole('banner');
    await userEvent.click(within(banner).getByRole('button', { name: 'Add novel' }));
    expect(screen.getByRole('dialog', { name: 'Add novel' })).toBeInTheDocument();
  });
});
