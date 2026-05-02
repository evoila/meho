// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Badge component
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Badge } from '../Badge';

describe('Badge', () => {
  describe('rendering', () => {
    it('renders children correctly', () => {
      render(<Badge>Active</Badge>);
      expect(screen.getByText('Active')).toBeInTheDocument();
    });

    it('renders as a span element', () => {
      render(<Badge>Status</Badge>);
      expect(screen.getByText('Status').tagName).toBe('SPAN');
    });

    it('applies custom className', () => {
      render(<Badge className="custom-class">Test</Badge>);
      expect(screen.getByText('Test')).toHaveClass('custom-class');
    });

    it('has rounded-full corners', () => {
      render(<Badge>Test</Badge>);
      expect(screen.getByText('Test')).toHaveClass('rounded-full');
    });
  });

  describe('variants', () => {
    it('applies default variant styles', () => {
      render(<Badge variant="default">Default</Badge>);
      expect(screen.getByText('Default')).toHaveClass('bg-surface-hover', 'text-text-secondary');
    });

    it('applies primary variant styles', () => {
      render(<Badge variant="primary">Primary</Badge>);
      const badge = screen.getByText('Primary');
      expect(badge).toHaveClass('text-primary-400');
    });

    it('applies success variant styles', () => {
      render(<Badge variant="success">Success</Badge>);
      const badge = screen.getByText('Success');
      expect(badge).toHaveClass('text-mint-400');
    });

    it('applies warning variant styles', () => {
      render(<Badge variant="warning">Warning</Badge>);
      const badge = screen.getByText('Warning');
      expect(badge).toHaveClass('text-maize-400');
    });

    it('applies error variant styles', () => {
      render(<Badge variant="error">Error</Badge>);
      const badge = screen.getByText('Error');
      expect(badge).toHaveClass('text-red-400');
    });

    it('applies info variant styles', () => {
      render(<Badge variant="info">Info</Badge>);
      const badge = screen.getByText('Info');
      expect(badge).toHaveClass('text-blue-400');
    });
  });

  describe('sizes', () => {
    it('applies small size by default', () => {
      render(<Badge>Small</Badge>);
      expect(screen.getByText('Small')).toHaveClass('px-2', 'py-0.5', 'text-xs');
    });

    it('applies medium size', () => {
      render(<Badge size="md">Medium</Badge>);
      expect(screen.getByText('Medium')).toHaveClass('px-2.5', 'py-1', 'text-sm');
    });
  });

  describe('dot indicator', () => {
    it('does not show dot by default', () => {
      const { container } = render(<Badge>No Dot</Badge>);
      const dot = container.querySelector('.w-1\\.5.h-1\\.5.rounded-full');
      expect(dot).not.toBeInTheDocument();
    });

    it('shows dot when dot prop is true', () => {
      const { container } = render(<Badge dot>With Dot</Badge>);
      const dot = container.querySelector('.rounded-full.w-1\\.5');
      expect(dot).toBeInTheDocument();
    });

    it('applies default dot color for default variant', () => {
      const { container } = render(<Badge variant="default" dot>Default</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-text-tertiary');
    });

    it('applies primary dot color', () => {
      const { container } = render(<Badge variant="primary" dot>Primary</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-primary-500');
    });

    it('applies success dot color', () => {
      const { container } = render(<Badge variant="success" dot>Success</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-mint-500');
    });

    it('applies warning dot color', () => {
      const { container } = render(<Badge variant="warning" dot>Warning</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-maize-500');
    });

    it('applies error dot color', () => {
      const { container } = render(<Badge variant="error" dot>Error</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-red-500');
    });

    it('applies info dot color', () => {
      const { container } = render(<Badge variant="info" dot>Info</Badge>);
      const dot = container.querySelector('.w-1\\.5');
      expect(dot).toHaveClass('bg-blue-500');
    });
  });

  describe('styling', () => {
    it('has inline-flex display', () => {
      render(<Badge>Inline</Badge>);
      expect(screen.getByText('Inline')).toHaveClass('inline-flex');
    });

    it('has items-center for vertical alignment', () => {
      render(<Badge>Aligned</Badge>);
      expect(screen.getByText('Aligned')).toHaveClass('items-center');
    });

    it('has gap between dot and text', () => {
      render(<Badge dot>With Gap</Badge>);
      expect(screen.getByText('With Gap')).toHaveClass('gap-1.5');
    });

    it('has font-medium weight', () => {
      render(<Badge>Bold</Badge>);
      expect(screen.getByText('Bold')).toHaveClass('font-medium');
    });
  });

  describe('ref forwarding', () => {
    it('forwards ref to span element', () => {
      const ref = { current: null as HTMLSpanElement | null };
      render(<Badge ref={ref}>Ref Test</Badge>);
      expect(ref.current).toBeInstanceOf(HTMLSpanElement);
    });
  });

  describe('real-world usage', () => {
    it('can be used as a status indicator', () => {
      render(
        <div>
          <Badge variant="success" dot>Online</Badge>
          <Badge variant="error" dot>Offline</Badge>
        </div>
      );
      expect(screen.getByText('Online')).toBeInTheDocument();
      expect(screen.getByText('Offline')).toBeInTheDocument();
    });

    it('can be used as a label', () => {
      render(
        <article>
          <h2>Article Title</h2>
          <Badge variant="primary">Featured</Badge>
          <Badge variant="info">New</Badge>
        </article>
      );
      expect(screen.getByText('Featured')).toBeInTheDocument();
      expect(screen.getByText('New')).toBeInTheDocument();
    });
  });
});

