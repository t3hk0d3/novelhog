import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { READER_SETTINGS_KEY, useReaderSettings } from '../hooks/useReaderSettings';
import { ReaderSettings, ThemeEditorDialog } from './ReaderSettings';

function Harness({ wide = true }: { wide?: boolean }) {
  const reader = useReaderSettings();
  const [creating, setCreating] = useState(false);
  return (
    <>
      <ReaderSettings
        reader={reader}
        wide={wide}
        onNewTheme={() => {
          setCreating(true);
        }}
      />
      {creating && (
        <ThemeEditorDialog
          reader={reader}
          onClose={() => {
            setCreating(false);
          }}
        />
      )}
    </>
  );
}

const stored = () =>
  JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) ?? '{}') as Record<string, unknown>;
const themes = () => screen.getByRole('group', { name: 'Theme' });
const fonts = () => screen.getByRole('group', { name: 'Font' });

beforeEach(() => {
  localStorage.clear();
});

describe('ReaderSettings', () => {
  describe('text size and width', () => {
    it('offers the four sizes and marks the chosen one', async () => {
      render(<Harness />);
      expect(screen.getByRole('button', { name: 'Medium text' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      await userEvent.click(screen.getByRole('button', { name: 'Large text' }));
      expect(screen.getByRole('button', { name: 'Large text' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });

    it('draws each size button at its own size (no competing text-size class)', () => {
      render(<Harness />);
      const sizeClasses = (name: string) =>
        screen.getByRole('button', { name }).className.match(/\btext-(?:sm|base|xl|2xl)\b/g) ?? [];
      expect(sizeClasses('Small text')).toEqual(['text-sm']);
      expect(sizeClasses('Medium text')).toEqual(['text-base']);
      expect(sizeClasses('Large text')).toEqual(['text-xl']);
      expect(sizeClasses('Extra large text')).toEqual(['text-2xl']);
    });

    it('puts the themes and the fonts side by side on desktop, four tiles to a row', () => {
      render(<Harness wide />);
      expect(themes().parentElement).toHaveClass('grid-cols-2');
      expect(fonts().parentElement).toBe(themes().parentElement);
      expect(themes().querySelector('.grid')).toHaveClass('grid-cols-4');
    });

    it('stacks them on a phone, four tiles to a row', () => {
      render(<Harness wide={false} />);
      expect(themes().parentElement).not.toHaveClass('grid-cols-2');
      expect(themes().querySelector('.grid')).toHaveClass('grid-cols-4');
    });

    it('offers the line width only when asked to', () => {
      const { unmount } = render(<Harness />);
      expect(screen.getByRole('button', { name: 'Wide' })).toBeInTheDocument();
      unmount();
      render(<Harness wide={false} />);
      expect(screen.queryByRole('button', { name: 'Wide' })).toBeNull();
    });
  });

  describe('themes', () => {
    it('lists Match app and the built-in themes, the app one chosen at first, none deletable', () => {
      render(<Harness />);
      const names = within(themes())
        .getAllByRole('button')
        .map((b) => b.getAttribute('aria-label') ?? b.textContent);
      expect(names).toEqual(
        expect.arrayContaining(['Match app', 'Paper', 'Sepia', 'Twilight', 'Night', 'Black']),
      );
      expect(within(themes()).getByRole('button', { name: 'Match app' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(within(themes()).queryByRole('button', { name: /^Delete theme/ })).toBeNull();
    });

    it('shows each theme as a small tile: "Aa" in its colours with its name under it', () => {
      render(<Harness />);
      const sepia = within(themes()).getByRole('button', { name: 'Sepia' });
      expect(sepia).toHaveTextContent('Aa');
      expect(sepia).toHaveTextContent('Sepia');
      const box = within(sepia).getByText('Aa');
      expect(box.style.backgroundColor).toBe('rgb(244, 236, 216)');
      expect(box.style.color).toBe('rgb(67, 52, 34)');
    });

    it('lays the tiles out in a wrapping grid, not one row each', () => {
      render(<Harness />);
      expect(themes().querySelector('.grid')).not.toBeNull();
      expect(fonts().querySelector('.grid')).not.toBeNull();
    });

    it('offers Delete only for the selected custom theme', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      await userEvent.type(screen.getByLabelText('Theme name'), 'Mine');
      await userEvent.click(screen.getByRole('button', { name: 'Save theme' }));
      expect(screen.getByRole('button', { name: 'Delete theme Mine' })).toBeInTheDocument();
      await userEvent.click(within(themes()).getByRole('button', { name: 'Paper' }));
      expect(screen.queryByRole('button', { name: 'Delete theme Mine' })).toBeNull();
      await userEvent.click(within(themes()).getByRole('button', { name: 'Mine' }));
      expect(screen.getByRole('button', { name: 'Delete theme Mine' })).toBeInTheDocument();
    });

    it('picking a theme selects it and stores the choice', async () => {
      render(<Harness />);
      await userEvent.click(within(themes()).getByRole('button', { name: 'Sepia' }));
      expect(within(themes()).getByRole('button', { name: 'Sepia' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(stored().themeId).toBe('sepia');
    });

    it('New theme opens a dialog of its own, named New theme', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      const dialog = screen.getByRole('dialog', { name: 'New theme' });
      expect(within(dialog).getByLabelText('Theme name')).toBeInTheDocument();
      expect(within(dialog).getByLabelText('Background colour')).toBeInTheDocument();
      expect(within(dialog).getByLabelText('Text colour')).toBeInTheDocument();
    });

    it('New theme opens an editor; Save adds a theme named as typed, selected and stored', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      await userEvent.type(screen.getByLabelText('Theme name'), 'Mine');
      fireEvent.change(screen.getByLabelText('Background colour'), {
        target: { value: '#101010' },
      });
      fireEvent.change(screen.getByLabelText('Text colour'), { target: { value: '#eeeeee' } });
      await userEvent.click(screen.getByRole('button', { name: 'Save theme' }));
      expect(within(themes()).getByRole('button', { name: 'Mine' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(screen.queryByRole('dialog', { name: 'New theme' })).toBeNull();
      expect(stored().customThemes).toMatchObject([
        { name: 'Mine', page: '#101010', ink: '#eeeeee' },
      ]);
    });

    it('Save is disabled until the theme has a name', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      expect(screen.getByRole('button', { name: 'Save theme' })).toBeDisabled();
      await userEvent.type(screen.getByLabelText('Theme name'), 'x');
      expect(screen.getByRole('button', { name: 'Save theme' })).toBeEnabled();
    });

    it('shows the contrast and warns when the text would be hard to read', () => {
      render(<Harness />);
      fireEvent.click(screen.getByRole('button', { name: 'New theme' }));
      fireEvent.change(screen.getByLabelText('Background colour'), {
        target: { value: '#000000' },
      });
      fireEvent.change(screen.getByLabelText('Text colour'), { target: { value: '#ffffff' } });
      expect(screen.getByText('Contrast 21.0 : 1')).toBeInTheDocument();
      expect(screen.queryByText(/hard to read/)).toBeNull();
      fireEvent.change(screen.getByLabelText('Text colour'), { target: { value: '#111111' } });
      expect(screen.getByText(/hard to read/)).toBeInTheDocument();
    });

    it('Cancel closes the editor without saving', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      await userEvent.type(screen.getByLabelText('Theme name'), 'Nope');
      await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(screen.queryByRole('dialog', { name: 'New theme' })).toBeNull();
      expect(within(themes()).queryByRole('button', { name: 'Nope' })).toBeNull();
    });

    it('a custom theme can be deleted, and the app theme is chosen again', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'New theme' }));
      await userEvent.type(screen.getByLabelText('Theme name'), 'Mine');
      await userEvent.click(screen.getByRole('button', { name: 'Save theme' }));
      await userEvent.click(screen.getByRole('button', { name: 'Delete theme Mine' }));
      expect(within(themes()).queryByRole('button', { name: 'Mine' })).toBeNull();
      expect(within(themes()).getByRole('button', { name: 'Match app' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });
  });

  describe('fonts', () => {
    it('lists the built-in fonts, each shown in its own face, the default chosen', () => {
      render(<Harness />);
      const sans = within(fonts()).getByRole('button', { name: 'Sans' });
      expect(sans).toHaveAttribute('aria-pressed', 'true');
      expect(within(fonts()).getByRole('button', { name: 'Serif' }).style.fontFamily).toContain(
        'Charter',
      );
      expect(within(fonts()).queryByRole('button', { name: /^Delete font/ })).toBeNull();
    });

    it('shows each font as a small tile: "Aa" in that face with its name under it', () => {
      render(<Harness />);
      const mono = within(fonts()).getByRole('button', { name: 'Mono' });
      expect(mono).toHaveTextContent('Aa');
      expect(mono).toHaveTextContent('Mono');
      expect(mono.style.fontFamily).toContain('monospace');
    });

    it('offers Delete only for the selected custom font', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      await userEvent.type(screen.getByLabelText('Font family'), 'Literata');
      await userEvent.click(screen.getByRole('button', { name: 'Save font' }));
      expect(screen.getByRole('button', { name: 'Delete font Literata' })).toBeInTheDocument();
      await userEvent.click(within(fonts()).getByRole('button', { name: 'Serif' }));
      expect(screen.queryByRole('button', { name: 'Delete font Literata' })).toBeNull();
    });

    it('picking a font selects it and stores the choice', async () => {
      render(<Harness />);
      await userEvent.click(within(fonts()).getByRole('button', { name: 'Serif' }));
      expect(within(fonts()).getByRole('button', { name: 'Serif' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(stored().fontId).toBe('serif');
    });

    it('Add font takes a family name and a fallback, then selects the new font', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      await userEvent.type(screen.getByLabelText('Font family'), 'Literata');
      await userEvent.selectOptions(screen.getByLabelText('Fallback'), 'serif');
      await userEvent.click(screen.getByRole('button', { name: 'Save font' }));
      expect(within(fonts()).getByRole('button', { name: 'Literata' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(stored().customFonts).toMatchObject([{ family: 'Literata', generic: 'serif' }]);
    });

    it('says why an unsafe family name is refused and saves nothing', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      await userEvent.type(screen.getByLabelText('Font family'), 'A;B');
      await userEvent.click(screen.getByRole('button', { name: 'Save font' }));
      expect(screen.getByRole('alert')).toHaveTextContent(/letters, numbers/i);
      expect(stored().customFonts).toBeUndefined();
    });

    it('a custom font can be deleted, and the default font is chosen again', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      await userEvent.type(screen.getByLabelText('Font family'), 'Literata');
      await userEvent.click(screen.getByRole('button', { name: 'Save font' }));
      await userEvent.click(screen.getByRole('button', { name: 'Delete font Literata' }));
      expect(within(fonts()).queryByRole('button', { name: 'Literata' })).toBeNull();
      expect(within(fonts()).getByRole('button', { name: 'Sans' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    });

    it('notes that a custom font must be installed on the device', async () => {
      render(<Harness />);
      await userEvent.click(screen.getByRole('button', { name: 'Add font' }));
      expect(screen.getByText(/installed on this device/i)).toBeInTheDocument();
    });
  });
});
