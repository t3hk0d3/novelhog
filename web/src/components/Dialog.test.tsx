import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { Dialog } from './Dialog';

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        onClick={() => {
          setOpen(true);
        }}
      >
        Open
      </button>
      {open && (
        <Dialog
          title="Demo"
          onClose={() => {
            setOpen(false);
          }}
        >
          <input aria-label="first" data-autofocus />
          <button>last</button>
        </Dialog>
      )}
    </>
  );
}

async function open() {
  render(<Harness />);
  await userEvent.click(screen.getByRole('button', { name: 'Open' }));
}

describe('Dialog', () => {
  it('opens as a native modal dialog named by its title', async () => {
    await open();
    const dialog = screen.getByRole('dialog', { name: 'Demo' });
    expect(dialog.tagName).toBe('DIALOG');
    expect(dialog).toHaveAttribute('open');
  });

  it('focuses the element marked data-autofocus', async () => {
    await open();
    expect(screen.getByLabelText('first')).toHaveFocus();
  });

  it('keeps focus inside: Tab from the last control wraps to the first, Shift+Tab back', async () => {
    await open();
    screen.getByRole('button', { name: 'last' }).focus();
    await userEvent.tab();
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(screen.getByRole('button', { name: 'last' })).toHaveFocus();
  });

  it('closes on Escape', async () => {
    await open();
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('closes with the Close button', async () => {
    await open();
    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('returns focus to the opener when it closes', async () => {
    await open();
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('button', { name: 'Open' })).toHaveFocus();
  });

  it('anchors to the bottom edge as a sheet', () => {
    render(
      <Dialog title="Sheet" sheet onClose={() => undefined}>
        body
      </Dialog>,
    );
    expect(screen.getByRole('dialog', { name: 'Sheet' }).className).toContain('mt-auto');
  });
});
