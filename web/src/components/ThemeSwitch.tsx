import { useTheme, type ThemeChoice } from '../theme/useTheme';
import { MonitorIcon, MoonIcon, SunIcon } from './icons';

const LABELS: Record<ThemeChoice, string> = {
  system: 'System',
  light: 'Light',
  dark: 'Dark',
};

const NEXT: Record<ThemeChoice, ThemeChoice> = {
  system: 'light',
  light: 'dark',
  dark: 'system',
};

function ChoiceIcon({ choice }: { choice: ThemeChoice }) {
  if (choice === 'light') return <SunIcon />;
  if (choice === 'dark') return <MoonIcon />;
  return <MonitorIcon />;
}

export function ThemeSwitch() {
  const { choice, setChoice } = useTheme();
  const label = LABELS[choice];

  return (
    <button
      type="button"
      aria-label={`Theme: ${label}`}
      title={`Theme: ${label}`}
      className="group inline-flex size-9 items-center justify-center rounded-full text-ink transition-colors duration-200 hover:bg-line hover:text-accent"
      onClick={() => {
        setChoice(NEXT[choice]);
      }}
    >
      <span className="flex items-center justify-center transition duration-200 group-hover:scale-105 group-hover:text-accent">
        <ChoiceIcon choice={choice} />
      </span>
    </button>
  );
}

export { ThemeSwitch as ThemeMenu };
