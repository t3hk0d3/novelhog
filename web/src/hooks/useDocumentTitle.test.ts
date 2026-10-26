import { renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { useDocumentTitle } from './useDocumentTitle';

beforeEach(() => {
  document.title = 'novelhog';
});

describe('useDocumentTitle', () => {
  it('puts the name in front of the app name', () => {
    renderHook(() => {
      useDocumentTitle('The Fake Chronicle');
    });
    expect(document.title).toBe('The Fake Chronicle · novelhog');
  });

  it('follows the name when it changes', () => {
    const { rerender } = renderHook(
      ({ name }: { name: string }) => {
        useDocumentTitle(name);
      },
      { initialProps: { name: 'Old name' } },
    );
    rerender({ name: 'New name' });
    expect(document.title).toBe('New name · novelhog');
  });

  it('puts the old title back when the page goes away', () => {
    const { unmount } = renderHook(() => {
      useDocumentTitle('The Fake Chronicle');
    });
    unmount();
    expect(document.title).toBe('novelhog');
  });

  it('leaves the title alone while there is no name yet', () => {
    renderHook(() => {
      useDocumentTitle(null);
    });
    expect(document.title).toBe('novelhog');
  });

  it('gives the title back when the name goes away again', () => {
    const { rerender } = renderHook<undefined, { name: string | null }>(
      ({ name }) => {
        useDocumentTitle(name);
      },
      { initialProps: { name: 'A' } },
    );
    rerender({ name: null });
    expect(document.title).toBe('novelhog');
  });
});
