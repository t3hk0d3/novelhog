import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ThemeMenu } from './ThemeMenu';

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

describe('ThemeMenu', () => {
  it('the button is labelled "Theme: <choice>" and has the menu popup', () => {
    localStorage.setItem('novelhog.theme', 'dark');
    render(<ThemeMenu />);
    const button = screen.getByRole('button', { name: 'Theme: dark' });
    expect(button).toHaveAttribute('aria-haspopup', 'menu');
    expect(button).toHaveAttribute('data-icon', 'moon');
  });

  it.each([
    ['system', 'monitor'],
    ['light', 'sun'],
    ['dark', 'moon'],
  ])('shows the %s icon (%s)', (choice, icon) => {
    localStorage.setItem('novelhog.theme', choice);
    render(<ThemeMenu />);
    expect(screen.getByRole('button', { name: `Theme: ${choice}` })).toHaveAttribute(
      'data-icon',
      icon,
    );
  });

  it('the menu has three menuitemradio entries with the current one checked', async () => {
    render(<ThemeMenu />);
    await userEvent.click(screen.getByRole('button', { name: 'Theme: system' }));
    const items = screen.getAllByRole('menuitemradio');
    expect(items.map((i) => i.textContent)).toEqual(['System', 'Light', 'Dark']);
    expect(items.map((i) => i.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false']);
  });

  it('choosing an entry changes the theme and closes the menu', async () => {
    render(<ThemeMenu />);
    await userEvent.click(screen.getByRole('button', { name: 'Theme: system' }));
    await userEvent.click(screen.getByRole('menuitemradio', { name: 'Light' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Theme: light' })).toBeInTheDocument();
  });

  it('Escape closes the menu', async () => {
    render(<ThemeMenu />);
    await userEvent.click(screen.getByRole('button', { name: 'Theme: system' }));
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});
