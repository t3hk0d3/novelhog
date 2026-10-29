import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from 'react';

const FOCUSABLE =
  'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

interface DialogProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** Anchor to the bottom edge as a sheet (phone layouts) instead of centring. */
  sheet?: boolean;
  /** A wide dialog (desktop) for content that wants columns; the default is a narrow column. */
  large?: boolean;
}

/**
 * A modal dialog on a native `<dialog>` opened with `showModal()`. Render it only while it is
 * open: mounting opens it, unmounting closes it and returns focus to the element that had focus
 * before. Escape (the native `cancel` event) and the Close button call `onClose`; the owner
 * unmounts it. Mark the element that should start focused with `data-autofocus`.
 */
export function Dialog({ title, onClose, children, sheet = false, large = false }: DialogProps) {
  const titleId = useId();
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const opener = document.activeElement;
    dialog.showModal();
    dialog.querySelector<HTMLElement>('[data-autofocus]')?.focus();
    return () => {
      if (dialog.open) dialog.close();
      if (opener instanceof HTMLElement) opener.focus();
    };
  }, []);

  // Native modals already keep focus inside; wrapping Tab here makes that explicit and does not
  // depend on the browser's inert handling.
  function wrapTab(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== 'Tab') return;
    const items = [...event.currentTarget.querySelectorAll<HTMLElement>(FOCUSABLE)];
    const first = items[0];
    const last = items[items.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onKeyDown={wrapTab}
      className={`w-full bg-surface text-ink shadow-xl backdrop:bg-black/50 ${
        sheet
          ? 'fixed inset-x-0 bottom-0 top-auto mx-0 mb-0 mt-auto max-h-[calc(100dvh-2rem)] max-w-none overflow-y-auto rounded-b-none rounded-t-2xl p-6 pb-[max(1.5rem,calc(1.5rem+env(safe-area-inset-bottom)))] transition-transform duration-200 starting:translate-y-full motion-reduce:transition-none'
          : `m-auto rounded-2xl p-6 ${large ? 'max-w-3xl' : 'max-w-lg'}`
      }`}
    >
      <div className="flex items-center justify-between">
        <h2 id={titleId} className="text-xl font-bold">
          {title}
        </h2>
        <button type="button" aria-label="Close" onClick={onClose} className="px-2 text-xl">
          ×
        </button>
      </div>
      {children}
    </dialog>
  );
}
