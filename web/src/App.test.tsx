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
  it.each(['/', '/novels/NV123'])('renders the header with the theme menu on %s', (path) => {
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
