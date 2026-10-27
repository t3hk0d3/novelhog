import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { ThemeSwitch } from './ThemeSwitch';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('ThemeSwitch', () => {
  it('the button is labelled "Theme: <Choice>" and is not a popup menu', () => {
    localStorage.setItem('novelhog.theme', 'dark');
    render(<ThemeSwitch />);
    const button = screen.getByRole('button', { name: 'Theme: Dark' });
    expect(button).toBeInTheDocument();
    expect(button).not.toHaveAttribute('aria-haspopup');
    expect(button).not.toHaveAttribute('aria-expanded');
  });

  it.each([
    ['system', 'monitor', 'System'],
    ['light', 'sun', 'Light'],
    ['dark', 'moon', 'Dark'],
  ])('renders the %s choice as the %s icon with label "Theme: %s"', (choice, icon, label) => {
    localStorage.setItem('novelhog.theme', choice);
    render(<ThemeSwitch />);
    const button = screen.getByRole('button', { name: `Theme: ${label}` });
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', icon);
    expect(button).toHaveAttribute('title', `Theme: ${label}`);
  });

  it('clicking cycles through system -> light -> dark -> system', async () => {
    render(<ThemeSwitch />);
    const button = screen.getByRole('button', { name: 'Theme: System' });
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'monitor');
    expect(document.documentElement).not.toHaveAttribute('data-theme');

    // Click 1: System -> Light
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: 'Theme: Light' })).toBeInTheDocument();
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'sun');
    expect(localStorage.getItem('novelhog.theme')).toBe('light');
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');

    // Click 2: Light -> Dark
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: 'Theme: Dark' })).toBeInTheDocument();
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'moon');
    expect(localStorage.getItem('novelhog.theme')).toBe('dark');
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');

    // Click 3: Dark -> System
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: 'Theme: System' })).toBeInTheDocument();
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'monitor');
    expect(localStorage.getItem('novelhog.theme')).toBe('system');
    expect(document.documentElement).not.toHaveAttribute('data-theme');

    // Click 4: System -> Light again
    await userEvent.click(button);
    expect(screen.getByRole('button', { name: 'Theme: Light' })).toBeInTheDocument();
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', 'sun');
    expect(localStorage.getItem('novelhog.theme')).toBe('light');
  });

  it('does not open any dropdown menu when clicked', async () => {
    render(<ThemeSwitch />);
    await userEvent.click(screen.getByRole('button', { name: 'Theme: System' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.queryByRole('menuitemradio')).not.toBeInTheDocument();
  });

  it('can be activated via keyboard', async () => {
    render(<ThemeSwitch />);
    const button = screen.getByRole('button', { name: 'Theme: System' });
    button.focus();
    expect(button).toHaveFocus();
    await userEvent.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: 'Theme: Light' })).toBeInTheDocument();
    await userEvent.keyboard(' ');
    expect(screen.getByRole('button', { name: 'Theme: Dark' })).toBeInTheDocument();
  });
});
