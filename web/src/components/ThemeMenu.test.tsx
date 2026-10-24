import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';
import { ThemeMenu } from './ThemeMenu';

beforeEach(() => {
  localStorage.clear();
});

async function openMenu(name = 'Theme: System') {
  await userEvent.click(screen.getByRole('button', { name }));
}

describe('ThemeMenu', () => {
  it('the button is labelled "Theme: <Choice>" and has the menu popup', () => {
    localStorage.setItem('novelhog.theme', 'dark');
    render(<ThemeMenu />);
    expect(screen.getByRole('button', { name: 'Theme: Dark' })).toHaveAttribute(
      'aria-haspopup',
      'menu',
    );
  });

  it.each([
    ['system', 'monitor'],
    ['light', 'sun'],
    ['dark', 'moon'],
  ])('renders the %s choice as the %s icon', (choice, icon) => {
    localStorage.setItem('novelhog.theme', choice);
    render(<ThemeMenu />);
    const button = screen.getByRole('button', { name: /^Theme: / });
    expect(button.querySelector('svg')).toHaveAttribute('data-icon', icon);
  });

  it('the menu has three menuitemradio entries with the current one checked', async () => {
    render(<ThemeMenu />);
    await openMenu();
    const items = screen.getAllByRole('menuitemradio');
    expect(items.map((i) => i.textContent)).toEqual(['System', 'Light', 'Dark']);
    expect(items.map((i) => i.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false']);
  });

  it('choosing an entry changes the theme and closes the menu', async () => {
    render(<ThemeMenu />);
    await openMenu();
    await userEvent.click(screen.getByRole('menuitemradio', { name: 'Light' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Theme: Light' })).toBeInTheDocument();
  });

  it('Escape closes the menu and returns focus to the button', async () => {
    render(<ThemeMenu />);
    await openMenu();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Theme: System' })).toHaveFocus();
  });

  describe('keyboard (WAI-ARIA menu button)', () => {
    it('the button points at the menu with aria-controls', async () => {
      render(<ThemeMenu />);
      await openMenu();
      const menu = screen.getByRole('menu');
      expect(screen.getByRole('button', { name: 'Theme: System' })).toHaveAttribute(
        'aria-controls',
        menu.id,
      );
    });

    it('only the focused item is in the Tab order (roving tabindex)', async () => {
      render(<ThemeMenu />);
      await openMenu();
      const items = screen.getAllByRole('menuitemradio');
      expect(items.map((i) => i.getAttribute('tabindex'))).toEqual(['0', '-1', '-1']);
      await userEvent.keyboard('{ArrowDown}');
      expect(items.map((i) => i.getAttribute('tabindex'))).toEqual(['-1', '0', '-1']);
      expect(items[1]).toHaveFocus();
    });

    it('Home and End move to the first and last item', async () => {
      render(<ThemeMenu />);
      await openMenu();
      await userEvent.keyboard('{End}');
      expect(screen.getByRole('menuitemradio', { name: 'Dark' })).toHaveFocus();
      await userEvent.keyboard('{Home}');
      expect(screen.getByRole('menuitemradio', { name: 'System' })).toHaveFocus();
    });

    it('Tab closes the menu', async () => {
      render(<ThemeMenu />);
      await openMenu();
      await userEvent.tab();
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    it('closes when focus moves outside the menu', async () => {
      render(
        <>
          <ThemeMenu />
          <button>elsewhere</button>
        </>,
      );
      await openMenu();
      act(() => {
        screen.getByRole('button', { name: 'elsewhere' }).focus();
      });
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
  });
});
