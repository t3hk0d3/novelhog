import { describe, expect, it } from 'vitest';
import { dateOnly, relative } from './time';

const now = new Date('2026-10-19T12:00:00.000Z');
const ago = (ms: number) => new Date(now.getTime() - ms);
const MIN = 60_000;

describe('relative', () => {
  it('gives "12 min ago" style strings', () => {
    expect(relative(ago(12 * MIN), now)).toBe('12 min ago');
    expect(relative(ago(2 * 60 * MIN), now)).toBe('2 h ago');
    expect(relative(ago(3 * 24 * 60 * MIN), now)).toBe('3 d ago');
  });

  it('says "just now" under a minute', () => {
    expect(relative(ago(30_000), now)).toBe('just now');
  });

  it('accepts an RFC 3339 string', () => {
    expect(relative('2026-10-19T11:48:00.000Z', now)).toBe('12 min ago');
  });

  it('describes the future with "in"', () => {
    expect(relative(new Date(now.getTime() + 4 * 60 * MIN), now)).toBe('in 4 h');
  });
});

describe('dateOnly', () => {
  it('gives the local date only', () => {
    const d = new Date(2026, 9, 5, 23, 59);
    expect(dateOnly(d)).toBe('2026-10-05');
  });
});
