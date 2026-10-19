import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(resolve(__dirname, 'tokens.css'), 'utf8');
type Rgb = [number, number, number];

function token(name: string, theme: 0 | 1): Rgb {
  const match = new RegExp(`--${name}:\\s*light-dark\\((#\\w{6}),\\s*(#\\w{6})\\)`).exec(css);
  if (!match) throw new Error(`token --${name} missing`);
  const hex = match[theme + 1] ?? '';
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const [lr = 0, lg = 0, lb = 0] = [r, g, b].map((v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * lr + 0.7152 * lg + 0.0722 * lb;
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return ((hi ?? 0) + 0.05) / ((lo ?? 0) + 0.05);
}

function over(fg: Rgb, bg: Rgb, alpha: number): Rgb {
  return fg.map((v, i) => Math.round(v * alpha + (bg[i] ?? 0) * (1 - alpha))) as Rgb;
}

describe.each([
  [0, 'light'],
  [1, 'dark'],
] as const)('contrast in the %s theme', (theme, name) => {
  it(`gold-ink on the 15% gold tint of a surface is AA (${name})`, () => {
    const surface = token('surface', theme);
    const tint = over(token('gold', theme), surface, 0.15);
    expect(contrast(token('gold-ink', theme), tint)).toBeGreaterThanOrEqual(4.5);
  });

  it(`danger text on surface and page is AA (${name})`, () => {
    for (const bg of ['surface', 'page']) {
      expect(contrast(token('danger', theme), token(bg, theme))).toBeGreaterThanOrEqual(4.5);
    }
  });
});
