import { describe, expect, it } from 'vitest';
import { BUILT_IN_FONTS, cleanFamily, customStack, parseCustomFonts } from './readerFonts';

describe('BUILT_IN_FONTS', () => {
  it('have unique ids and end in a generic family', () => {
    expect(new Set(BUILT_IN_FONTS.map((f) => f.id)).size).toBe(BUILT_IN_FONTS.length);
    for (const font of BUILT_IN_FONTS) {
      expect(font.stack).toMatch(/(serif|sans-serif|monospace)$/);
    }
  });
});

describe('cleanFamily', () => {
  it.each([
    ['Literata', 'Literata'],
    ['  Source Serif 4  ', 'Source Serif 4'],
    ['Noto Sans JP', 'Noto Sans JP'],
    ['Игра', 'Игра'],
  ])('accepts %s', (raw, cleaned) => {
    expect(cleanFamily(raw)).toBe(cleaned);
  });

  it.each([
    [''],
    ['   '],
    ['x'.repeat(61)],
    ['A"; color: red'],
    ["Evil'Font"],
    ['a{b}'],
    ['url(http://x/y)'],
    ['back\\slash'],
    ['<script>'],
  ])('rejects %j', (raw) => {
    expect(cleanFamily(raw)).toBeNull();
  });
});

describe('customStack', () => {
  it('quotes the family and falls back to the generic one', () => {
    expect(customStack('Source Serif 4', 'serif')).toBe('"Source Serif 4", serif');
  });
});

describe('parseCustomFonts', () => {
  const good = { id: 'custom-f1', family: 'Literata', generic: 'serif' };

  it('keeps well-formed fonts', () => {
    expect(parseCustomFonts([good])).toEqual([good]);
  });

  it('returns none when it is not an array', () => {
    expect(parseCustomFonts('x')).toEqual([]);
    expect(parseCustomFonts(null)).toEqual([]);
  });

  it('drops entries with an unsafe family, an unknown generic or a foreign id', () => {
    expect(
      parseCustomFonts([
        { ...good, family: 'A"; x' },
        { ...good, generic: 'cursive' },
        { ...good, id: 'serif' },
        null,
        good,
      ]),
    ).toEqual([good]);
  });

  it('keeps the first of two entries with the same id, and at most 20', () => {
    expect(parseCustomFonts([good, { ...good, family: 'Other' }])).toEqual([good]);
    const many = Array.from({ length: 30 }, (_, i) => ({ ...good, id: `custom-${String(i)}` }));
    expect(parseCustomFonts(many)).toHaveLength(20);
  });
});
