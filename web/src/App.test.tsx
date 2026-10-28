import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
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
  it.each(['/', '/novels/NV123'])('renders the header with the theme switch on %s', (path) => {
    render(
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Theme: System' })).toBeInTheDocument();
  });

  it('renders the novel page route', () => {
    render(
      <MemoryRouter initialEntries={['/novels/NV123']}>
        <App />
      </MemoryRouter>,
    );
    expect(screen.getByRole('main')).toHaveTextContent('Connecting…');
  });
});

describe('App layout', () => {
  it('fills the screen, so a short page (the reader) can put its bar on the bottom edge', () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    const main = screen.getByRole('main');
    expect(main).toHaveClass('flex-1', 'flex', 'flex-col');
    expect(main.parentElement).toHaveClass('min-h-dvh', 'flex', 'flex-col');
  });
});

describe('App header', () => {
  it('Add novel is an icon button that opens the add-novel dialog', async () => {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
    const banner = screen.getByRole('banner');
    const button = within(banner).getByRole('button', { name: 'Add novel' });
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'plus');
    expect(button).toHaveClass(
      'group',
      'size-9',
      'rounded-full',
      'hover:bg-line',
      'hover:text-accent',
    );
    await userEvent.click(button);
    expect(screen.getByRole('dialog', { name: 'Add novel' })).toBeInTheDocument();
  });
});

describe('App service worker', () => {
  function mountApp() {
    render(
      <MemoryRouter initialEntries={['/']}>
        <App />
      </MemoryRouter>,
    );
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, 'serviceWorker');
    vi.unstubAllGlobals();
  });

  function stubWorker() {
    const register = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { register, ready: new Promise(() => undefined) },
    });
    return register;
  }

  it('registers /sw.js in a secure context', () => {
    vi.stubGlobal('isSecureContext', true);
    const register = stubWorker();
    mountApp();
    expect(register).toHaveBeenCalledWith('/sw.js');
  });

  it('does not register over plain HTTP', () => {
    vi.stubGlobal('isSecureContext', false);
    const register = stubWorker();
    mountApp();
    expect(register).not.toHaveBeenCalled();
  });

  it('does nothing when the browser has no service workers', () => {
    vi.stubGlobal('isSecureContext', true);
    expect(() => {
      mountApp();
    }).not.toThrow();
  });
});
