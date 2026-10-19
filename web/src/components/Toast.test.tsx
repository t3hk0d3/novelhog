import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ToastProvider, useToast } from './Toast';

function Trigger() {
  const { show } = useToast();
  return (
    <button
      onClick={() => {
        show('Export queued');
      }}
    >
      go
    </button>
  );
}

afterEach(() => {
  vi.useRealTimers();
});

describe('Toast', () => {
  it('show() displays the text in a live region', async () => {
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    await userEvent.click(screen.getByRole('button', { name: 'go' }));
    expect(screen.getByRole('status')).toHaveTextContent('Export queued');
  });

  it('disappears after a few seconds', () => {
    vi.useFakeTimers();
    render(
      <ToastProvider>
        <Trigger />
      </ToastProvider>,
    );
    act(() => {
      screen.getByRole('button', { name: 'go' }).click();
    });
    expect(screen.getByRole('status')).toHaveTextContent('Export queued');
    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(screen.getByRole('status')).toBeEmptyDOMElement();
  });
});
