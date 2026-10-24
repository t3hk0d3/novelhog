import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTheme } from './useTheme';

beforeEach(() => {
  localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
});

describe('useTheme', () => {
  describe('with no stored choice', () => {
    it('choice is system and data-theme is left unset so CSS follows prefers-color-scheme', () => {
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('system');
      expect(document.documentElement).not.toHaveAttribute('data-theme');
    });
  });

  describe('with a stored choice light or dark', () => {
    it('applies data-theme on <html>', () => {
      localStorage.setItem('novelhog.theme', 'dark');
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('dark');
      expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    });

    it('stores a new choice and removes data-theme for system', () => {
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
      localStorage.setItem('novelhog.theme', 'purple');
      const { result } = renderHook(() => useTheme());
      expect(result.current.choice).toBe('system');
    });
  });

  describe('with two consumers', () => {
    it('keeps both in sync when one changes the choice', () => {
      const a = renderHook(() => useTheme());
      const b = renderHook(() => useTheme());
      act(() => {
        a.result.current.setChoice('dark');
      });
      expect(a.result.current.choice).toBe('dark');
      expect(b.result.current.choice).toBe('dark');
    });
  });

  describe('when the OS scheme changes while on system', () => {
    it('leaves data-theme unset so the CSS media query updates live', () => {
      const { result } = renderHook(() => useTheme());
      act(() => {
        result.current.setChoice('dark');
      });
      act(() => {
        result.current.setChoice('system');
      });
      expect(document.documentElement).not.toHaveAttribute('data-theme');
    });
  });

  describe('when localStorage throws', () => {
    it('falls back to system without crashing', () => {
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
