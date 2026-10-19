import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useTheme } from './useTheme';

type Listener = (event: { matches: boolean }) => void;

function mockScheme(initialDark: boolean) {
  let dark = initialDark;
  const listeners = new Set<Listener>();
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      get matches() {
        return dark;
      },
      addEventListener: (_: string, l: Listener) => listeners.add(l),
      removeEventListener: (_: string, l: Listener) => listeners.delete(l),
    })),
  );
  return (next: boolean) => {
    dark = next;
    listeners.forEach((l) => {
      l({ matches: next });
    });
  };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe('useTheme', () => {
  describe('with no stored choice', () => {
    it('choice is system and follows prefers-color-scheme', () => {
      mockScheme(true);
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('system');
      expect(result.current.resolved).toBe('dark');
      expect(document.documentElement).not.toHaveAttribute('data-theme');
    });
  });

  describe('with a stored choice light or dark', () => {
    it('applies data-theme on <html>', () => {
      mockScheme(false);
      localStorage.setItem('novelhog.theme', 'dark');
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('dark');
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    });

    it('stores a new choice and removes data-theme for system', () => {
      mockScheme(false);
      const { result } = renderHook(() => useTheme());
      act(() => {
        result.current.setChoice('light');
      });
      expect(localStorage.getItem('novelhog.theme')).toBe('light');
      expect(document.documentElement).toHaveAttribute('data-theme', 'light');
      act(() => {
        result.current.setChoice('system');
      });
      expect(document.documentElement).not.toHaveAttribute('data-theme');
    });

    it('ignores an unknown stored value', () => {
      mockScheme(false);
      localStorage.setItem('novelhog.theme', 'purple');
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('system');
    });
  });

  describe('when the OS scheme changes while on system', () => {
    it('updates live', () => {
      const change = mockScheme(false);
      const { result } = renderHook(() => useTheme());
      expect(result.current.resolved).toBe('light');
      act(() => {
        change(true);
      });
      expect(result.current.resolved).toBe('dark');
    });
  });

  describe('when localStorage throws', () => {
    it('falls back to system without crashing', () => {
      mockScheme(false);
      vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
        throw new Error('blocked');
      });
      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
        throw new Error('blocked');
      });
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('system');
      act(() => {
        result.current.setChoice('dark');
      });
      expect(result.current.choice).toBe('dark');
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    });
  });
});
