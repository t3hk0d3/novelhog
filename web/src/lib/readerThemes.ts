/** Colour themes of the reader: built-in ones, the ones the user saves, and what they set. */

export interface ThemeDef {
  id: string;
  name: string;
  /** Page background, `#rrggbb`. */
  page: string;
  /** Text colour, `#rrggbb`. */
  ink: string;
  /** Selected buttons and the like; the text colour when absent. */
  accent?: string;
}

/** A theme the user made: only the two colours, the rest is derived. */
export interface CustomTheme {
  id: string;
  name: string;
  page: string;
  ink: string;
}

/** The reader follows the app's own light/dark theme. It is not in the list below. */
export const APP_THEME_ID = 'app';
export const CUSTOM_ID_PREFIX = 'custom-';
export const MAX_CUSTOM = 20;
export const MAX_NAME = 30;
/** WCAG AA for body text. */
export const MIN_CONTRAST = 4.5;

export const BUILT_IN_THEMES: ThemeDef[] = [
  { id: 'paper', name: 'Paper', page: '#fbf8f1', ink: '#26231f' },
  { id: 'sepia', name: 'Sepia', page: '#f4ecd8', ink: '#433422', accent: '#8a4b1f' },
  { id: 'twilight', name: 'Twilight', page: '#1f2333', ink: '#d5d9e8', accent: '#9db4ff' },
  { id: 'night', name: 'Night', page: '#14161a', ink: '#c5c9d0', accent: '#8fb4f0' },
  { id: 'black', name: 'Black', page: '#000000', ink: '#b3b3b3' },
];

const HEX = /^#[0-9a-f]{6}$/i;

export function isHexColor(value: unknown): value is string {
  return typeof value === 'string' && HEX.test(value);
}

function luminance(hex: string): number {
  const [red = 0, green = 0, blue = 0] = [1, 3, 5].map((start) => {
    const channel = parseInt(hex.slice(start, start + 2), 16) / 255;
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

/** WCAG contrast ratio of two `#rrggbb` colours, 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((light ?? 0) + 0.05) / ((dark ?? 0) + 0.05);
}

export function isDark(hex: string): boolean {
  return luminance(hex) < 0.18;
}

/**
 * The CSS properties that retheme the page: the app's own tokens (so every utility class follows)
 * and the colour scheme. Everything but page and ink is mixed from those two in CSS.
 */
export function themeStyle(theme: ThemeDef): Record<string, string> {
  const { page, ink } = theme;
  return {
    '--page': page,
    '--ink': ink,
    '--surface': `color-mix(in srgb, ${ink} 6%, ${page})`,
    '--ink-muted': `color-mix(in srgb, ${ink} 62%, ${page})`,
    '--line': `color-mix(in srgb, ${ink} 18%, ${page})`,
    '--accent': theme.accent ?? ink,
    '--accent-ink': page,
    'color-scheme': isDark(page) ? 'dark' : 'light',
  };
}

/** Stored custom themes, keeping only well-formed ones (the storage is not trusted). */
export function parseCustomThemes(value: unknown): CustomTheme[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const themes: CustomTheme[] = [];
  for (const item of value as unknown[]) {
    if (typeof item !== 'object' || item === null) continue;
    const { id, name, page, ink } = item as Record<string, unknown>;
    if (typeof id !== 'string' || !id.startsWith(CUSTOM_ID_PREFIX) || seen.has(id)) continue;
    if (typeof name !== 'string' || name.length < 1 || name.length > MAX_NAME) continue;
    if (!isHexColor(page) || !isHexColor(ink)) continue;
    seen.add(id);
    themes.push({ id, name, page, ink });
    if (themes.length === MAX_CUSTOM) break;
  }
  return themes;
}
