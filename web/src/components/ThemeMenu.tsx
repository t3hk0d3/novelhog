import { useEffect, useId, useRef, useState, type FocusEvent, type KeyboardEvent } from 'react';
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
  const [focused, setFocused] = useState(0);
  const menuId = useId();
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const label = OPTIONS.find((o) => o.value === choice)?.label ?? 'System';

  const openMenu = () => {
    setFocused(OPTIONS.findIndex((o) => o.value === choice));
    setOpen(true);
  };

  useEffect(() => {
    if (open) items.current[focused]?.focus();
    // focus when the menu opens or the roving position moves
  }, [open, focused]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open]);

  const close = () => {
    setOpen(false);
    button.current?.focus();
  };

  const onKeyDown = (event: KeyboardEvent) => {
    if (!open) return;
    const last = OPTIONS.length - 1;
    switch (event.key) {
      case 'Escape':
        event.preventDefault();
        close();
        break;
      case 'Tab':
        setOpen(false);
        break;
      case 'ArrowDown':
        event.preventDefault();
        setFocused((i) => (i >= last ? 0 : i + 1));
        break;
      case 'ArrowUp':
        event.preventDefault();
        setFocused((i) => (i <= 0 ? last : i - 1));
        break;
      case 'Home':
        event.preventDefault();
        setFocused(0);
        break;
      case 'End':
        event.preventDefault();
        setFocused(last);
        break;
    }
  };

  const onBlur = (event: FocusEvent) => {
    const next = event.relatedTarget;
    if (next instanceof Node && !root.current?.contains(next)) setOpen(false);
  };

  return (
    <div ref={root} className="relative" onKeyDown={onKeyDown} onBlur={onBlur}>
      <button
        ref={button}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={`Theme: ${label}`}
        className="inline-flex size-9 items-center justify-center rounded-full text-ink hover:bg-line"
        onClick={() => {
          if (open) setOpen(false);
          else openMenu();
        }}
      >
        <ChoiceIcon choice={choice} />
      </button>
      {open && (
        <div
          id={menuId}
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
              tabIndex={index === focused ? 0 : -1}
              className="flex w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-line"
              onFocus={() => {
                setFocused(index);
              }}
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
