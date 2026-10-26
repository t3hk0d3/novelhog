import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const manifest = JSON.parse(
  readFileSync(resolve(__dirname, '../public/manifest.webmanifest'), 'utf8'),
) as {
  name: string;
  display: string;
  start_url: string;
  icons: { src: string; sizes: string }[];
};

describe('manifest.webmanifest', () => {
  it('is the standalone app novelhog starting at /', () => {
    expect(manifest).toMatchObject({ name: 'novelhog', display: 'standalone', start_url: '/' });
  });

  it('has the 192 and 512 icons, and the files exist', () => {
    expect(manifest.icons.map((icon) => icon.sizes)).toEqual(['192x192', '512x512']);
    for (const icon of manifest.icons) {
      const png = readFileSync(resolve(__dirname, '../public', icon.src.slice(1)));
      const size = Number(icon.sizes.split('x')[0]);
      expect(png.readUInt32BE(16)).toBe(size);
      expect(png.readUInt32BE(20)).toBe(size);
    }
  });
});
