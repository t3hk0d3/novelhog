import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { ThemeSwitch } from './ThemeSwitch';

interface HeaderProps {
  /** "Add novel" button (Task 33). */
  addNovel?: ReactNode;
  /** Notifications bell (Task 39); sits immediately right of the theme switcher. */
  bell?: ReactNode;
}

export function Header({ addNovel, bell }: HeaderProps) {
  return (
    <header className="flex items-center gap-2 border-b border-line bg-surface px-4 py-2">
      <Link to="/" className="mr-auto text-lg font-semibold text-ink">
        novelhog
      </Link>
      {addNovel}
      <ThemeSwitch />
      {bell}
    </header>
  );
}
