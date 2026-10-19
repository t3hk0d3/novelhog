import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { exportFilename } from './filename';

interface Case {
  name: string;
  title: string | null;
  start: number;
  end: number;
  ext: string;
  expected: string;
}

const cases = JSON.parse(
  readFileSync(join(import.meta.dirname, '../../../tests/fixtures/filenames.json'), 'utf8'),
) as Case[];

describe('exportFilename', () => {
  it('has the shared fixture cases', () => {
    expect(cases.length).toBeGreaterThan(20);
  });

  it.each(cases)('matches domain.exports.export_filename: $name', (c) => {
    expect(exportFilename(c.title, c.start, c.end, c.ext)).toBe(c.expected);
  });

  it('keeps the whole name below 255 bytes', () => {
    const name = exportFilename('é'.repeat(400), 1, 99999, 'epub');
    expect(new TextEncoder().encode(name).length).toBeLessThan(255);
  });
});
