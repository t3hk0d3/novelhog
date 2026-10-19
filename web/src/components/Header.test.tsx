import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { Header } from './Header';

function renderHeader(props: Parameters<typeof Header>[0] = {}) {
  return render(
    <MemoryRouter>
      <Header {...props} />
    </MemoryRouter>,
  );
}

describe('Header', () => {
  it('is a banner with a brand link to the library', () => {
    renderHeader();
    expect(screen.getByRole('banner')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'novelhog' })).toHaveAttribute('href', '/');
  });

  it('shows the theme menu', () => {
    renderHeader();
    expect(screen.getByRole('button', { name: /^Theme: / })).toBeInTheDocument();
  });

  it('orders the slots: add novel, theme menu, then the bell', () => {
    renderHeader({
      addNovel: <button>Add novel</button>,
      bell: <button>Bell</button>,
    });
    const names = screen
      .getAllByRole('button')
      .map((b) => b.getAttribute('aria-label') ?? b.textContent);
    expect(names).toEqual(['Add novel', 'Theme: System', 'Bell']);
  });
});
