import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { BUILT_IN_FONTS } from '../lib/readerFonts';
import { BUILT_IN_THEMES } from '../lib/readerThemes';
import { READER_SETTINGS_KEY, useReaderSettings } from './useReaderSettings';

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

const stored = () => JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) ?? 'null') as unknown;
const MINE = { name: 'Mine', page: '#101010', ink: '#eeeeee' };

describe('useReaderSettings', () => {
  describe('first use, nothing stored', () => {
    it('uses the default size, width, app theme and sans font, with nothing custom', () => {
      const { result } = renderHook(() => useReaderSettings());
      expect(result.current.settings).toEqual({
        fontSize: 'medium',
        width: 'medium',
        themeId: 'app',
        fontId: 'sans',
        customThemes: [],
        customFonts: [],
      });
      expect(result.current.theme).toBeNull();
      expect(result.current.fontStack).toBe(BUILT_IN_FONTS[0]?.stack);
    });
  });

  describe('changing a setting', () => {
    it('updates the value', () => {
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setFontSize('xlarge');
      });
      act(() => {
        result.current.setWidth('wide');
      });
      expect(result.current.settings).toMatchObject({ fontSize: 'xlarge', width: 'wide' });
    });

    it('stores everything under one key', () => {
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setFontSize('small');
      });
      expect(stored()).toMatchObject({ fontSize: 'small', width: 'medium' });
    });

    it('is read back by the next mount', () => {
      const first = renderHook(() => useReaderSettings());
      act(() => {
        first.result.current.setWidth('narrow');
      });
      first.unmount();
      const second = renderHook(() => useReaderSettings());
      expect(second.result.current.settings.width).toBe('narrow');
    });
  });

  describe('theme', () => {
    it('setTheme picks a built-in theme and exposes its colours', () => {
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setTheme('sepia');
      });
      expect(result.current.settings.themeId).toBe('sepia');
      expect(result.current.theme).toEqual(BUILT_IN_THEMES.find((t) => t.id === 'sepia'));
    });

    it('setTheme ignores an id it does not know', () => {
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setTheme('nope');
      });
      expect(result.current.settings.themeId).toBe('app');
    });

    it('saveTheme adds a custom theme, selects it, stores it and returns it', () => {
      const { result } = renderHook(() => useReaderSettings());
      let saved: ReturnType<typeof result.current.saveTheme> = null;
      act(() => {
        saved = result.current.saveTheme(MINE);
      });
      expect(saved).toMatchObject(MINE);
      expect(result.current.settings.customThemes).toEqual([saved]);
      expect(result.current.settings.themeId).toBe((saved as unknown as { id: string }).id);
      expect(result.current.theme).toMatchObject(MINE);
      expect(stored()).toMatchObject({ customThemes: [MINE] });
    });

    it('saveTheme trims the name and refuses a bad colour or an empty name', () => {
      const { result } = renderHook(() => useReaderSettings());
      let outcomes: unknown[] = [];
      act(() => {
        outcomes = [
          result.current.saveTheme({ ...MINE, name: '  Spaced  ' }),
          result.current.saveTheme({ ...MINE, page: 'red' }),
          result.current.saveTheme({ ...MINE, name: '   ' }),
        ];
      });
      expect(outcomes[0]).toMatchObject({ name: 'Spaced' });
      expect(outcomes[1]).toBeNull();
      expect(outcomes[2]).toBeNull();
      expect(result.current.settings.customThemes).toHaveLength(1);
    });

    it('saveTheme refuses a 21st theme', () => {
      const { result } = renderHook(() => useReaderSettings());
      for (let i = 0; i < 20; i++) {
        act(() => {
          result.current.saveTheme({ ...MINE, name: `T${String(i)}` });
        });
      }
      let extra: unknown;
      act(() => {
        extra = result.current.saveTheme(MINE);
      });
      expect(extra).toBeNull();
      expect(result.current.settings.customThemes).toHaveLength(20);
    });

    it('deleteTheme removes it, and the selection falls back to the app theme', () => {
      const { result } = renderHook(() => useReaderSettings());
      let id = '';
      act(() => {
        id = result.current.saveTheme(MINE)?.id ?? '';
      });
      act(() => {
        result.current.deleteTheme(id);
      });
      expect(result.current.settings.customThemes).toEqual([]);
      expect(result.current.settings.themeId).toBe('app');
      expect(result.current.theme).toBeNull();
    });

    it('deleteTheme keeps another theme selected', () => {
      const { result } = renderHook(() => useReaderSettings());
      let id = '';
      act(() => {
        id = result.current.saveTheme(MINE)?.id ?? '';
      });
      act(() => {
        result.current.setTheme('night');
      });
      act(() => {
        result.current.deleteTheme(id);
      });
      expect(result.current.settings.themeId).toBe('night');
    });

    it('is read back, custom themes included', () => {
      const first = renderHook(() => useReaderSettings());
      act(() => {
        first.result.current.saveTheme(MINE);
      });
      first.unmount();
      const second = renderHook(() => useReaderSettings());
      expect(second.result.current.theme).toMatchObject(MINE);
    });
  });

  describe('font', () => {
    it('setFont picks a built-in stack', () => {
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setFont('serif');
      });
      expect(result.current.settings.fontId).toBe('serif');
      expect(result.current.fontStack).toBe(BUILT_IN_FONTS.find((f) => f.id === 'serif')?.stack);
    });

    it('saveFont adds a custom family, selects it and builds its stack', () => {
      const { result } = renderHook(() => useReaderSettings());
      let saved: ReturnType<typeof result.current.saveFont> = null;
      act(() => {
        saved = result.current.saveFont({ family: '  Literata ', generic: 'serif' });
      });
      expect(saved).toMatchObject({ family: 'Literata', generic: 'serif' });
      expect(result.current.fontStack).toBe('"Literata", serif');
      expect(stored()).toMatchObject({ customFonts: [{ family: 'Literata' }] });
    });

    it('saveFont refuses an unsafe family', () => {
      const { result } = renderHook(() => useReaderSettings());
      let saved: unknown;
      act(() => {
        saved = result.current.saveFont({ family: 'A"; color: red', generic: 'serif' });
      });
      expect(saved).toBeNull();
      expect(result.current.settings.customFonts).toEqual([]);
    });

    it('deleteFont removes it, and the selection falls back to the default', () => {
      const { result } = renderHook(() => useReaderSettings());
      let id = '';
      act(() => {
        id = result.current.saveFont({ family: 'Literata', generic: 'serif' })?.id ?? '';
      });
      act(() => {
        result.current.deleteFont(id);
      });
      expect(result.current.settings.fontId).toBe('sans');
      expect(result.current.settings.customFonts).toEqual([]);
    });
  });

  describe('stored value is malformed or has unknown values', () => {
    const defaults = {
      fontSize: 'medium',
      width: 'medium',
      themeId: 'app',
      fontId: 'sans',
      customThemes: [],
      customFonts: [],
    };

    it.each([['not json'], [JSON.stringify({ fontSize: 'huge', width: 5 })], ['null']])(
      'falls back to the defaults for %s',
      (value) => {
        localStorage.setItem(READER_SETTINGS_KEY, value);
        const { result } = renderHook(() => useReaderSettings());
        expect(result.current.settings).toEqual(defaults);
      },
    );

    it('keeps the valid half of a partly valid value', () => {
      localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify({ fontSize: 'large', width: 'x' }));
      const { result } = renderHook(() => useReaderSettings());
      expect(result.current.settings).toEqual({ ...defaults, fontSize: 'large' });
    });

    it('still reads what an earlier version stored (size and width only)', () => {
      localStorage.setItem(
        READER_SETTINGS_KEY,
        JSON.stringify({ fontSize: 'xlarge', width: 'wide' }),
      );
      const { result } = renderHook(() => useReaderSettings());
      expect(result.current.settings).toEqual({ ...defaults, fontSize: 'xlarge', width: 'wide' });
    });

    it('drops malformed custom entries and falls back when the chosen theme or font is gone', () => {
      localStorage.setItem(
        READER_SETTINGS_KEY,
        JSON.stringify({
          themeId: 'custom-gone',
          fontId: 'custom-gone',
          customThemes: [{ id: 'custom-1', name: 'Bad', page: 'red', ink: '#000000' }],
          customFonts: [{ id: 'custom-1', family: 'A"; x', generic: 'serif' }],
        }),
      );
      const { result } = renderHook(() => useReaderSettings());
      expect(result.current.settings).toEqual(defaults);
    });
  });

  describe('storage throws (private mode)', () => {
    it('works in memory and does not throw', () => {
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('denied');
      });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('denied');
      });
      const { result } = renderHook(() => useReaderSettings());
      act(() => {
        result.current.setFontSize('large');
      });
      act(() => {
        result.current.saveTheme(MINE);
      });
      expect(result.current.settings.fontSize).toBe('large');
      expect(result.current.settings.customThemes).toHaveLength(1);
    });
  });
});
