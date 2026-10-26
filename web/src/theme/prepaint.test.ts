import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import { prepaintScript } from './prepaint';

function run(): void {
  // Executes the same source the build inlines into index.html.
  window.eval(prepaintScript);
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('prepaint script', () => {
  it('sets data-theme = dark before the app module loads', () => {
    localStorage.setItem('novelhog.theme', 'dark');
    run();
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  });

  it('leaves data-theme unset for system or an unknown value', () => {
    localStorage.setItem('novelhog.theme', 'system');
    run();
    expect(document.documentElement).not.toHaveAttribute('data-theme');
  });

  it('is injected from the marker in index.html', () => {
    const html = readFileSync(join(import.meta.dirname, '../../index.html'), 'utf8');
    expect(html).toContain('<!-- prepaint -->');
  });
});
