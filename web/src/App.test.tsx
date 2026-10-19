import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';

beforeEach(() => {
  localStorage.clear();
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
    expect(screen.getByRole('button', { name: 'Theme: system' })).toBeInTheDocument();
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
