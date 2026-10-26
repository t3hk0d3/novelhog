import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
});

// jsdom has no <dialog> modal behaviour (showModal/close), so emulate the parts the app relies on:
// showModal opens the dialog, close() closes it and fires `close`, and Escape fires a cancelable
// `cancel` on the open dialog that closes it unless prevented, as browsers do. Native focus
// trapping (inertness of the rest of the page) is NOT emulated; Dialog also wraps Tab itself.
HTMLDialogElement.prototype.showModal = function showModal() {
  this.setAttribute('open', '');
};
HTMLDialogElement.prototype.close = function close() {
  if (!this.hasAttribute('open')) return;
  this.removeAttribute('open');
  this.dispatchEvent(new Event('close'));
};
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const dialog = document.querySelector('dialog[open]');
  if (!dialog) return;
  const cancel = new Event('cancel', { cancelable: true });
  dialog.dispatchEvent(cancel);
  if (!cancel.defaultPrevented) (dialog as HTMLDialogElement).close();
});

// jsdom has no matchMedia; default to "no query matches" (phone layout). Tests override it.
if (typeof window.matchMedia !== 'function') {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    }) as unknown as MediaQueryList;
}
