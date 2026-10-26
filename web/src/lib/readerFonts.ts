/** Font choices of the reader: system stacks, and families the user names. */

export type Generic = 'serif' | 'sans-serif' | 'monospace';
export const GENERICS: readonly Generic[] = ['serif', 'sans-serif', 'monospace'];

export interface FontDef {
  id: string;
  name: string;
  /** A CSS `font-family` value. */
  stack: string;
}

/** A family the user typed in; it must be installed on the device that reads. */
export interface CustomFont {
  id: string;
  family: string;
  generic: Generic;
}

export const DEFAULT_FONT_ID = 'sans';
export const CUSTOM_ID_PREFIX = 'custom-';
export const MAX_CUSTOM = 20;
const MAX_FAMILY = 60;

// System stacks only: nothing is downloaded, so they work offline and cost nothing.
export const BUILT_IN_FONTS: FontDef[] = [
  { id: 'sans', name: 'Sans', stack: "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif" },
  {
    id: 'humanist',
    name: 'Humanist',
    stack: "Seravek, 'Gill Sans Nova', Ubuntu, Calibri, 'DejaVu Sans', sans-serif",
  },
  {
    id: 'serif',
    name: 'Serif',
    stack: "Charter, 'Bitstream Charter', 'Sitka Text', Cambria, Georgia, serif",
  },
  {
    id: 'old-style',
    name: 'Old style',
    stack: "'Iowan Old Style', 'Palatino Linotype', 'URW Palladio L', P052, Palatino, serif",
  },
  {
    id: 'mono',
    name: 'Mono',
    stack: "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, monospace",
  },
];

// Letters, digits and a few separators: nothing that could end the CSS value or add to it.
const SAFE_FAMILY = /^[\p{L}\p{N} .\-_]+$/u;

/** The family name trimmed, or null when it is empty, too long or has unsafe characters. */
export function cleanFamily(raw: string): string | null {
  const family = raw.trim();
  if (family.length < 1 || family.length > MAX_FAMILY) return null;
  return SAFE_FAMILY.test(family) ? family : null;
}

export function customStack(family: string, generic: Generic): string {
  return `"${family}", ${generic}`;
}

/** Stored custom fonts, keeping only well-formed ones (the storage is not trusted). */
export function parseCustomFonts(value: unknown): CustomFont[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const fonts: CustomFont[] = [];
  for (const item of value as unknown[]) {
    if (typeof item !== 'object' || item === null) continue;
    const { id, family, generic } = item as Record<string, unknown>;
    if (typeof id !== 'string' || !id.startsWith(CUSTOM_ID_PREFIX) || seen.has(id)) continue;
    if (typeof family !== 'string' || cleanFamily(family) !== family) continue;
    const known = GENERICS.find((g) => g === generic);
    if (known === undefined) continue;
    seen.add(id);
    fonts.push({ id, family, generic: known });
    if (fonts.length === MAX_CUSTOM) break;
  }
  return fonts;
}
