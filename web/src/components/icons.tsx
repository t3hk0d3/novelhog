import type { ReactNode } from 'react';

interface IconProps {
  name?: string;
  className?: string;
  children: ReactNode;
}

function Svg({ name, className, children }: IconProps) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="20"
      height="20"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-icon={name}
      className={className}
    >
      {children}
    </svg>
  );
}

export function MonitorIcon() {
  return (
    <Svg name="monitor">
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </Svg>
  );
}

export function SunIcon() {
  return (
    <Svg name="sun">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </Svg>
  );
}

export function MoonIcon() {
  return (
    <Svg name="moon">
      <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
    </Svg>
  );
}

export function CheckIcon() {
  return (
    <Svg name="check" className="size-4">
      <path d="M5 12l5 5 9-10" />
    </Svg>
  );
}
