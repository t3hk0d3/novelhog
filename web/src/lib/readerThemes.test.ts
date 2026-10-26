import { describe, expect, it } from 'vitest';
import {
  BUILT_IN_THEMES,
  contrastRatio,
  isHexColor,
  isDark,
  parseCustomThemes,
  themeStyle,
} from './readerThemes';

describe('isHexColor', () => {
  it.each([['#aabbcc'], ['#AABBCC'], ['#000000']])('accepts %s', (value) => {
    expect(isHexColor(value)).toBe(true);
  });

  it.each([['#abc'], ['red'], ['#gggggg'], ['#aabbccdd'], ['url(x)'], ['']])(
    'rejects %s',
    (value) => {
      expect(isHexColor(value)).toBe(false);
    },
  );
});

describe('contrastRatio', () => {
  it('is 21 for black on white, either way round', () => {
    expect(contrastRatio('#000000', '#ffffff')).toBeCloseTo(21, 5);
    expect(contrastRatio('#ffffff', '#000000')).toBeCloseTo(21, 5);
  });

  it('is 1 for the same colour', () => {
    expect(contrastRatio('#336699', '#336699')).toBeCloseTo(1, 5);
  });
});

describe('isDark', () => {
  it('tells dark backgrounds from light ones', () => {
    expect(isDark('#14161a')).toBe(true);
    expect(isDark('#fbf8f1')).toBe(false);
  });
});

describe('BUILT_IN_THEMES', () => {
  it('have unique ids, valid colours and readable text (at least 4.5:1)', () => {
    expect(new Set(BUILT_IN_THEMES.map((t) => t.id)).size).toBe(BUILT_IN_THEMES.length);
    for (const theme of BUILT_IN_THEMES) {
      expect(isHexColor(theme.page)).toBe(true);
      expect(isHexColor(theme.ink)).toBe(true);
      expect(contrastRatio(theme.page, theme.ink)).toBeGreaterThanOrEqual(4.5);
    }
  });
});

describe('themeStyle', () => {
  const theme = { id: 'x', name: 'X', page: '#fbf8f1', ink: '#26231f' };

  it('sets the page and ink tokens and derives the rest from them', () => {
    const style = themeStyle(theme);
    expect(style['--page']).toBe('#fbf8f1');
    expect(style['--ink']).toBe('#26231f');
    expect(style['--surface']).toContain('color-mix');
    expect(style['--ink-muted']).toContain('#26231f');
    expect(style['--line']).toContain('#fbf8f1');
  });

  it('uses the ink as accent unless the theme names one, with the page as its ink', () => {
    expect(themeStyle(theme)['--accent']).toBe('#26231f');
    expect(themeStyle({ ...theme, accent: '#8a4b1f' })['--accent']).toBe('#8a4b1f');
    expect(themeStyle(theme)['--accent-ink']).toBe('#fbf8f1');
  });

  it('declares the colour scheme of the page so controls and scrollbars match', () => {
    expect(themeStyle(theme)['color-scheme']).toBe('light');
    expect(themeStyle({ ...theme, page: '#101010', ink: '#eeeeee' })['color-scheme']).toBe('dark');
  });
});

describe('parseCustomThemes', () => {
  const good = { id: 'custom-a1', name: 'Mine', page: '#101010', ink: '#eeeeee' };

  it('keeps well-formed themes', () => {
    expect(parseCustomThemes([good])).toEqual([good]);
  });

  it.each([
    ['not an array', 'nope'],
    ['null', null],
    ['an object', {}],
  ])('returns none for %s', (_name, value) => {
    expect(parseCustomThemes(value)).toEqual([]);
  });

  it('drops entries with a bad colour, a missing or odd name, or an id outside the custom- space', () => {
    expect(
      parseCustomThemes([
        { ...good, page: 'red' },
        { ...good, ink: 'url(x)' },
        { ...good, name: '' },
        { ...good, name: 'x'.repeat(31) },
        { ...good, id: 'paper' },
        { ...good, id: 5 },
        null,
        'x',
        good,
      ]),
    ).toEqual([good]);
  });

  it('keeps the first of two entries with the same id', () => {
    expect(parseCustomThemes([good, { ...good, name: 'Other' }])).toEqual([good]);
  });

  it('keeps at most 20', () => {
    const many = Array.from({ length: 30 }, (_, i) => ({ ...good, id: `custom-${String(i)}` }));
    expect(parseCustomThemes(many)).toHaveLength(20);
  });
});
