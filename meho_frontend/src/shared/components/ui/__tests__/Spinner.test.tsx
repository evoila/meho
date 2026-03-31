// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Spinner component
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Spinner } from '../Spinner';

describe('Spinner', () => {
  describe('rendering', () => {
    it('renders svg element', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toBeInTheDocument();
    });

    it('has animate-spin class', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toHaveClass('animate-spin');
    });

    it('applies custom className', () => {
      render(<Spinner className="custom-class" />);
      expect(screen.getByRole('status').parentElement).toHaveClass('custom-class');
    });
  });

  describe('sizes', () => {
    it('applies xs size', () => {
      render(<Spinner size="xs" />);
      expect(screen.getByRole('status')).toHaveClass('h-3', 'w-3');
    });

    it('applies sm size', () => {
      render(<Spinner size="sm" />);
      expect(screen.getByRole('status')).toHaveClass('h-4', 'w-4');
    });

    it('applies md size by default', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toHaveClass('h-6', 'w-6');
    });

    it('applies lg size', () => {
      render(<Spinner size="lg" />);
      expect(screen.getByRole('status')).toHaveClass('h-8', 'w-8');
    });

    it('applies xl size', () => {
      render(<Spinner size="xl" />);
      expect(screen.getByRole('status')).toHaveClass('h-12', 'w-12');
    });
  });

  describe('label', () => {
    it('does not show label by default', () => {
      render(<Spinner />);
      const container = screen.getByRole('status').parentElement;
      expect(container?.textContent).toBe('');
    });

    it('shows label when provided', () => {
      render(<Spinner label="Loading..." />);
      expect(screen.getByText('Loading...')).toBeInTheDocument();
    });

    it('applies label styling', () => {
      render(<Spinner label="Loading" />);
      expect(screen.getByText('Loading')).toHaveClass('text-text-secondary', 'text-sm');
    });
  });

  describe('accessibility', () => {
    it('has status role', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toBeInTheDocument();
    });

    it('has default aria-label', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Loading');
    });

    it('uses label prop as aria-label', () => {
      render(<Spinner label="Processing data" />);
      expect(screen.getByRole('status')).toHaveAttribute('aria-label', 'Processing data');
    });
  });

  describe('styling', () => {
    it('uses primary color', () => {
      render(<Spinner />);
      expect(screen.getByRole('status')).toHaveClass('text-primary-500');
    });

    it('has flex container for alignment', () => {
      render(<Spinner />);
      expect(screen.getByRole('status').parentElement).toHaveClass('flex', 'items-center');
    });

    it('has gap between spinner and label', () => {
      render(<Spinner label="Loading" />);
      expect(screen.getByRole('status').parentElement).toHaveClass('gap-2');
    });
  });

  describe('svg structure', () => {
    it('contains circle element for track', () => {
      const { container } = render(<Spinner />);
      const circle = container.querySelector('circle');
      expect(circle).toBeInTheDocument();
      expect(circle).toHaveClass('opacity-25');
    });

    it('contains path element for arc', () => {
      const { container } = render(<Spinner />);
      const path = container.querySelector('path');
      expect(path).toBeInTheDocument();
      expect(path).toHaveClass('opacity-75');
    });
  });

  describe('real-world usage', () => {
    it('can be used as loading indicator', () => {
      render(
        <button disabled>
          <Spinner size="sm" />
          Processing...
        </button>
      );
      expect(screen.getByRole('button')).toBeDisabled();
      expect(screen.getByRole('status')).toBeInTheDocument();
    });

    it('can be used as page loader', () => {
      render(
        <div className="flex justify-center p-8">
          <Spinner size="xl" label="Loading content..." />
        </div>
      );
      expect(screen.getByText('Loading content...')).toBeInTheDocument();
    });
  });
});

