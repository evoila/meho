// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for Modal component
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Modal } from '../Modal';

describe('Modal', () => {
  const defaultProps = {
    isOpen: true,
    onClose: vi.fn(),
    children: <div>Modal content</div>,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    // Clean up any body style changes
    document.body.style.overflow = '';
  });

  describe('rendering', () => {
    it('renders when isOpen is true', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByText('Modal content')).toBeInTheDocument();
    });

    it('does not render when isOpen is false', () => {
      render(<Modal {...defaultProps} isOpen={false} />);
      expect(screen.queryByText('Modal content')).not.toBeInTheDocument();
    });

    it('renders children correctly', () => {
      render(
        <Modal {...defaultProps}>
          <p>Custom content</p>
        </Modal>
      );
      expect(screen.getByText('Custom content')).toBeInTheDocument();
    });

    it('renders as a dialog', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByRole('dialog')).toBeInTheDocument();
    });

    it('has aria-modal attribute', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByRole('dialog')).toHaveAttribute('aria-modal', 'true');
    });
  });

  describe('title and description', () => {
    it('renders title when provided', () => {
      render(<Modal {...defaultProps} title="Modal Title" />);
      expect(screen.getByText('Modal Title')).toBeInTheDocument();
    });

    it('renders description when provided', () => {
      render(<Modal {...defaultProps} title="Title" description="Description text" />);
      expect(screen.getByText('Description text')).toBeInTheDocument();
    });

    it('has aria-labelledby when title is provided', () => {
      render(<Modal {...defaultProps} title="Modal Title" />);
      // Phase 84: Modal now uses useId() for unique aria-labelledby IDs (modal-title-{id})
      const dialog = screen.getByRole('dialog');
      const labelledBy = dialog.getAttribute('aria-labelledby');
      expect(labelledBy).toBeTruthy();
      expect(labelledBy).toContain('modal-title');
    });

    it('does not have aria-labelledby when no title', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByRole('dialog')).not.toHaveAttribute('aria-labelledby');
    });

    it('applies correct title styling', () => {
      render(<Modal {...defaultProps} title="Title" />);
      expect(screen.getByText('Title')).toHaveClass('text-lg', 'font-semibold', 'text-text-primary');
    });

    it('applies correct description styling', () => {
      render(<Modal {...defaultProps} title="Title" description="Description" />);
      expect(screen.getByText('Description')).toHaveClass('text-sm', 'text-text-secondary');
    });
  });

  describe('close button', () => {
    it('shows close button by default', () => {
      render(<Modal {...defaultProps} title="Title" />);
      expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument();
    });

    it('hides close button when showCloseButton is false', () => {
      render(<Modal {...defaultProps} title="Title" showCloseButton={false} />);
      expect(screen.queryByRole('button', { name: 'Close' })).not.toBeInTheDocument();
    });

    it('calls onClose when close button is clicked', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} title="Title" onClose={onClose} />);

      await userEvent.click(screen.getByRole('button', { name: 'Close' }));

      expect(onClose).toHaveBeenCalledOnce();
    });
  });

  describe('overlay click', () => {
    it('calls onClose when overlay is clicked', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} onClose={onClose} />);

      // Click the backdrop (aria-hidden element)
      const backdrop = document.querySelector('[aria-hidden="true"]');
      if (backdrop) {
        await userEvent.click(backdrop);
      }

      expect(onClose).toHaveBeenCalledOnce();
    });

    it('does not call onClose when closeOnOverlayClick is false', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} onClose={onClose} closeOnOverlayClick={false} />);

      const backdrop = document.querySelector('[aria-hidden="true"]');
      if (backdrop) {
        await userEvent.click(backdrop);
      }

      expect(onClose).not.toHaveBeenCalled();
    });

    it('does not call onClose when clicking modal content', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} onClose={onClose} />);

      await userEvent.click(screen.getByText('Modal content'));

      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe('keyboard events', () => {
    it('calls onClose when ESC is pressed', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} onClose={onClose} />);

      await userEvent.keyboard('{Escape}');

      expect(onClose).toHaveBeenCalledOnce();
    });

    it('does not call onClose on ESC when closeOnEsc is false', async () => {
      const onClose = vi.fn();
      render(<Modal {...defaultProps} onClose={onClose} closeOnEsc={false} />);

      await userEvent.keyboard('{Escape}');

      expect(onClose).not.toHaveBeenCalled();
    });
  });

  describe('body scroll lock', () => {
    it('disables body scroll when open', () => {
      render(<Modal {...defaultProps} />);
      expect(document.body.style.overflow).toBe('hidden');
    });

    it('restores body scroll when closed', () => {
      const { rerender } = render(<Modal {...defaultProps} />);
      expect(document.body.style.overflow).toBe('hidden');

      rerender(<Modal {...defaultProps} isOpen={false} />);
      expect(document.body.style.overflow).toBe('');
    });

    it('restores body scroll on unmount', () => {
      const { unmount } = render(<Modal {...defaultProps} />);
      expect(document.body.style.overflow).toBe('hidden');

      unmount();
      expect(document.body.style.overflow).toBe('');
    });
  });

  describe('sizes', () => {
    it('applies sm size', () => {
      const { container } = render(<Modal {...defaultProps} size="sm" />);
      const panel = container.querySelector('.max-w-sm');
      expect(panel).toBeInTheDocument();
    });

    it('applies md size by default', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const panel = container.querySelector('.max-w-md');
      expect(panel).toBeInTheDocument();
    });

    it('applies lg size', () => {
      const { container } = render(<Modal {...defaultProps} size="lg" />);
      const panel = container.querySelector('.max-w-lg');
      expect(panel).toBeInTheDocument();
    });

    it('applies xl size', () => {
      const { container } = render(<Modal {...defaultProps} size="xl" />);
      const panel = container.querySelector('.max-w-xl');
      expect(panel).toBeInTheDocument();
    });

    it('applies full size', () => {
      const { container } = render(<Modal {...defaultProps} size="full" />);
      const panel = container.querySelector('.max-w-4xl');
      expect(panel).toBeInTheDocument();
    });
  });

  describe('footer', () => {
    it('renders footer when provided', () => {
      render(
        <Modal {...defaultProps} footer={<button>Save</button>} />
      );
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    });

    it('does not render footer section when not provided', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const footerBorder = container.querySelectorAll('.border-t.border-border');
      // Only header border should exist (if title is present)
      expect(footerBorder.length).toBeLessThanOrEqual(1);
    });

    it('renders multiple footer buttons', () => {
      render(
        <Modal
          {...defaultProps}
          footer={
            <>
              <button>Cancel</button>
              <button>Save</button>
            </>
          }
        />
      );
      expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
    });
  });

  describe('styling', () => {
    it('has fixed positioning', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByRole('dialog')).toHaveClass('fixed', 'inset-0');
    });

    it('has high z-index', () => {
      render(<Modal {...defaultProps} />);
      expect(screen.getByRole('dialog')).toHaveClass('z-50');
    });

    it('has backdrop with blur', () => {
      render(<Modal {...defaultProps} />);
      const backdrop = document.querySelector('[aria-hidden="true"]');
      expect(backdrop).toHaveClass('backdrop-blur-sm');
    });

    it('modal panel has rounded corners', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const panel = container.querySelector('.rounded-xl');
      expect(panel).toBeInTheDocument();
    });

    it('modal panel has shadow', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const panel = container.querySelector('.shadow-xl');
      expect(panel).toBeInTheDocument();
    });
  });

  describe('content scrolling', () => {
    it('content area has overflow-y-auto', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const content = container.querySelector('.overflow-y-auto');
      expect(content).toBeInTheDocument();
    });

    it('content area has max height', () => {
      const { container } = render(<Modal {...defaultProps} />);
      const content = container.querySelector('.max-h-\\[70vh\\]');
      expect(content).toBeInTheDocument();
    });
  });

  describe('real-world usage', () => {
    it('can be used as confirmation dialog', async () => {
      const onConfirm = vi.fn();
      const onCancel = vi.fn();

      render(
        <Modal
          isOpen={true}
          onClose={onCancel}
          title="Confirm Delete"
          description="Are you sure you want to delete this item?"
          footer={
            <>
              <button onClick={onCancel}>Cancel</button>
              <button onClick={onConfirm}>Delete</button>
            </>
          }
        >
          <p>This action cannot be undone.</p>
        </Modal>
      );

      await userEvent.click(screen.getByRole('button', { name: 'Delete' }));
      expect(onConfirm).toHaveBeenCalledOnce();
    });

    it('can be used as form modal', () => {
      render(
        <Modal
          isOpen={true}
          onClose={vi.fn()}
          title="Create Item"
          size="lg"
          footer={
            <>
              <button>Cancel</button>
              <button>Create</button>
            </>
          }
        >
          <form>
            <input type="text" placeholder="Name" />
            <textarea placeholder="Description" />
          </form>
        </Modal>
      );

      expect(screen.getByPlaceholderText('Name')).toBeInTheDocument();
      expect(screen.getByPlaceholderText('Description')).toBeInTheDocument();
    });
  });
});

