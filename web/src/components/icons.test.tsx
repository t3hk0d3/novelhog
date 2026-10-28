import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CheckIcon, MonitorIcon, MoonIcon, PlusIcon, SunIcon } from './icons';

describe('icons', () => {
  it.each([
    ['monitor', MonitorIcon],
    ['sun', SunIcon],
    ['moon', MoonIcon],
    ['check', CheckIcon],
    ['plus', PlusIcon],
  ])('%s is a decorative svg named by data-icon', (name, Icon) => {
    const { container } = render(<Icon />);
    const svg = container.querySelector('svg');
    expect(svg).toHaveAttribute('data-icon', name);
    expect(svg).toHaveAttribute('aria-hidden', 'true');
  });
});
