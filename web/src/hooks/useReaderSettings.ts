import { useRef, useState } from 'react';
import {
  BUILT_IN_FONTS,
  CUSTOM_ID_PREFIX as FONT_PREFIX,
  DEFAULT_FONT_ID,
  MAX_CUSTOM as MAX_FONTS,
  cleanFamily,
  customStack,
  parseCustomFonts,
  type CustomFont,
  type FontDef,
  type Generic,
} from '../lib/readerFonts';
import {
  APP_THEME_ID,
  BUILT_IN_THEMES,
  CUSTOM_ID_PREFIX as THEME_PREFIX,
  MAX_CUSTOM as MAX_THEMES,
  MAX_NAME,
  isHexColor,
  parseCustomThemes,
  type CustomTheme,
  type ThemeDef,
} from '../lib/readerThemes';

export const FONT_SIZES = ['small', 'medium', 'large', 'xlarge'] as const;
export const WIDTHS = ['narrow', 'medium', 'wide'] as const;

export type FontSize = (typeof FONT_SIZES)[number];
export type Width = (typeof WIDTHS)[number];

export interface ReaderSettings {
  fontSize: FontSize;
  width: Width;
  /** A built-in or custom theme id, or `app` to follow the app's own theme. */
  themeId: string;
  /** A built-in or custom font id. */
  fontId: string;
  customThemes: CustomTheme[];
  customFonts: CustomFont[];
}

export const READER_SETTINGS_KEY = 'novelhog.reader';

const DEFAULTS: ReaderSettings = {
  fontSize: 'medium',
  width: 'medium',
  themeId: APP_THEME_ID,
  fontId: DEFAULT_FONT_ID,
  customThemes: [],
  customFonts: [],
};

function oneOf<T extends string>(options: readonly T[], value: unknown, fallback: T): T {
  return options.find((option) => option === value) ?? fallback;
}

function customFontDef(font: CustomFont): FontDef {
  return { id: font.id, name: font.family, stack: customStack(font.family, font.generic) };
}

function findTheme(settings: ReaderSettings, id: string): ThemeDef | undefined {
  return [...BUILT_IN_THEMES, ...settings.customThemes].find((theme) => theme.id === id);
}

function findFont(settings: ReaderSettings, id: string): FontDef | undefined {
  return [...BUILT_IN_FONTS, ...settings.customFonts.map(customFontDef)].find(
    (font) => font.id === id,
  );
}

function load(): ReaderSettings {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(READER_SETTINGS_KEY) ?? 'null');
    if (typeof stored !== 'object' || stored === null) return DEFAULTS;
    const record = stored as Record<string, unknown>;
    const loaded: ReaderSettings = {
      fontSize: oneOf(FONT_SIZES, record.fontSize, DEFAULTS.fontSize),
      width: oneOf(WIDTHS, record.width, DEFAULTS.width),
      themeId: typeof record.themeId === 'string' ? record.themeId : DEFAULTS.themeId,
      fontId: typeof record.fontId === 'string' ? record.fontId : DEFAULTS.fontId,
      customThemes: parseCustomThemes(record.customThemes),
      customFonts: parseCustomFonts(record.customFonts),
    };
    // A choice whose theme or font is gone (or never existed) is the default again.
    return {
      ...loaded,
      themeId: findTheme(loaded, loaded.themeId) ? loaded.themeId : DEFAULTS.themeId,
      fontId: findFont(loaded, loaded.fontId) ? loaded.fontId : DEFAULTS.fontId,
    };
  } catch {
    return DEFAULTS;
  }
}

function save(settings: ReaderSettings) {
  try {
    localStorage.setItem(READER_SETTINGS_KEY, JSON.stringify(settings));
  } catch {
    // Without storage the choice only lasts for this visit.
  }
}

function newId(prefix: string): string {
  return `${prefix}${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * The reader's text size, line width, colour theme and font, kept in `localStorage` (and in
 * memory without it), together with the themes and fonts the user added. `theme` is null while
 * the reader follows the app's own theme.
 */
export function useReaderSettings() {
  const [settings, setSettings] = useState(load);
  // Calls made together (several saves in one event) must see each other's result.
  const latest = useRef(settings);
  const update = (next: ReaderSettings) => {
    latest.current = next;
    setSettings(next);
    save(next);
  };

  return {
    settings,
    theme: findTheme(settings, settings.themeId) ?? null,
    fontStack: (findFont(settings, settings.fontId) ?? BUILT_IN_FONTS[0])?.stack ?? 'sans-serif',
    /** Built-in themes, then the user's. */
    themes: [...BUILT_IN_THEMES, ...settings.customThemes] as ThemeDef[],
    /** Built-in fonts, then the user's. */
    fonts: [...BUILT_IN_FONTS, ...settings.customFonts.map(customFontDef)],
    setFontSize: (fontSize: FontSize) => {
      update({ ...latest.current, fontSize });
    },
    setWidth: (width: Width) => {
      update({ ...latest.current, width });
    },
    setTheme: (themeId: string) => {
      const now = latest.current;
      if (themeId === APP_THEME_ID || findTheme(now, themeId)) update({ ...now, themeId });
    },
    setFont: (fontId: string) => {
      const now = latest.current;
      if (findFont(now, fontId)) update({ ...now, fontId });
    },
    /** Adds and selects a theme; null when a colour or the name is not acceptable or the limit is reached. */
    saveTheme: (input: { name: string; page: string; ink: string }): CustomTheme | null => {
      const now = latest.current;
      const name = input.name.trim();
      if (name.length < 1 || name.length > MAX_NAME) return null;
      if (!isHexColor(input.page) || !isHexColor(input.ink)) return null;
      if (now.customThemes.length >= MAX_THEMES) return null;
      const theme: CustomTheme = {
        id: newId(THEME_PREFIX),
        name,
        page: input.page,
        ink: input.ink,
      };
      update({ ...now, customThemes: [...now.customThemes, theme], themeId: theme.id });
      return theme;
    },
    deleteTheme: (id: string) => {
      const now = latest.current;
      update({
        ...now,
        customThemes: now.customThemes.filter((theme) => theme.id !== id),
        themeId: now.themeId === id ? APP_THEME_ID : now.themeId,
      });
    },
    /** Adds and selects a font family; null when the name is not acceptable or the limit is reached. */
    saveFont: (input: { family: string; generic: Generic }): CustomFont | null => {
      const now = latest.current;
      const family = cleanFamily(input.family);
      if (family === null || now.customFonts.length >= MAX_FONTS) return null;
      const font: CustomFont = { id: newId(FONT_PREFIX), family, generic: input.generic };
      update({ ...now, customFonts: [...now.customFonts, font], fontId: font.id });
      return font;
    },
    deleteFont: (id: string) => {
      const now = latest.current;
      update({
        ...now,
        customFonts: now.customFonts.filter((font) => font.id !== id),
        fontId: now.fontId === id ? DEFAULT_FONT_ID : now.fontId,
      });
    },
  };
}
