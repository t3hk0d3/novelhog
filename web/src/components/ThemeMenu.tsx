import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useTheme, type ThemeChoice } from '../theme/useTheme';
import { CheckIcon, MonitorIcon, MoonIcon, SunIcon } from './icons';

const OPTIONS: { value: ThemeChoice; label: string }[] = [
  { value: 'system', label: 'System' },
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
];

function ChoiceIcon({ choice }: { choice: ThemeChoice }) {
  if (choice === 'light') return <SunIcon />;
  if (choice === 'dark') return <MoonIcon />;
  return <MonitorIcon />;
}

export function ThemeMenu() {
  const { choice, setChoice } = useTheme();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);

  useEffect(() => {
    if (!open) return;
    items.current[OPTIONS.findIndex((o) => o.value === choice)]?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
    };
    // focus only when the menu opens, not on every choice change
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const close = () => {
    setOpen(false);
    button.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
      return;
    }
    const step = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
    if (step === 0) return;
    event.preventDefault();
    const current = items.current.findIndex((el) => el === document.activeElement);
    const next = (current + step + OPTIONS.length) % OPTIONS.length;
    items.current[next]?.focus();
  };

  return (
    <div ref={root} className="relative" onKeyDown={onKeyDown}>
      <button
        ref={button}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Theme: ${choice}`}
        data-icon={choice === 'light' ? 'sun' : choice === 'dark' ? 'moon' : 'monitor'}
        className="inline-flex size-9 items-center justify-center rounded-full text-ink hover:bg-line"
        onClick={() => {
          setOpen((o) => !o);
        }}
      >
        <ChoiceIcon choice={choice} />
      </button>
      {open && (
        <div
          role="menu"
          aria-label="Theme"
          className="absolute right-0 z-10 mt-1 min-w-36 rounded-lg border border-line bg-surface p-1 shadow-lg"
        >
          {OPTIONS.map((option, index) => (
            <button
              key={option.value}
              ref={(el) => {
                items.current[index] = el;
              }}
              type="button"
              role="menuitemradio"
              aria-checked={option.value === choice}
              className="flex w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-line"
              onClick={() => {
                setChoice(option.value);
                close();
              }}
            >
              {option.label}
              {option.value === choice && <CheckIcon />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
