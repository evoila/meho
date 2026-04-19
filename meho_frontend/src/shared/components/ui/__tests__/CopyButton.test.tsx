// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * CopyButton Tests
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import { CopyButton } from '../CopyButton';

describe('CopyButton', () => {
  const originalClipboard = navigator.clipboard;

  beforeEach(() => {
    vi.useRealTimers();
    // Mock clipboard API
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.assign(navigator, {
      clipboard: originalClipboard,
    });
    vi.clearAllMocks();
  });

  it('renders copy button', () => {
    render(<CopyButton data="test" />);
    expect(screen.getByRole('button')).toBeInTheDocument();
  });

  it('copies string data to clipboard', async () => {
    render(<CopyButton data="Hello, world!" />);
    
    const button = screen.getByRole('button');
    fireEvent.click(button);

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith('Hello, world!');
    });
  });

  it('copies JSON stringified object to clipboard', async () => {
    const data = { name: 'test', value: 42 };
    render(<CopyButton data={data} />);

    const button = screen.getByRole('button');
    fireEvent.click(button);

    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        JSON.stringify(data, null, 2)
      );
    });
  });

  it('shows feedback after copying', async () => {
    render(<CopyButton data="test" />);

    const button = screen.getByRole('button');
    expect(button).toHaveAttribute('aria-label', 'Copy to clipboard');

    fireEvent.click(button);

    await waitFor(() => {
      expect(button).toHaveAttribute('aria-label', 'Copied!');
    });
  });

  it('resets feedback after 2 seconds', async () => {
    vi.useFakeTimers();
    render(<CopyButton data="test" />);

    const button = screen.getByRole('button');
    
    // Click and let the clipboard promise resolve
    await act(async () => {
      fireEvent.click(button);
      // Run microtasks only (not timers) to let clipboard promise resolve
      await Promise.resolve();
    });
    
    expect(button).toHaveAttribute('aria-label', 'Copied!');

    // Fast-forward 2 seconds
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(button).toHaveAttribute('aria-label', 'Copy to clipboard');

    vi.useRealTimers();
  });

  it('renders label when provided', () => {
    render(<CopyButton data="test" label="Copy JSON" />);
    expect(screen.getByText('Copy JSON')).toBeInTheDocument();
  });

  it('applies size variant', () => {
    const { container } = render(<CopyButton data="test" size="sm" />);
    expect(container.querySelector('.p-1')).toBeInTheDocument();
  });

  it('applies custom className', () => {
    const { container } = render(<CopyButton data="test" className="custom-class" />);
    expect(container.firstChild).toHaveClass('custom-class');
  });

  it('handles clipboard error gracefully', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    (navigator.clipboard.writeText as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Permission denied'));

    render(<CopyButton data="test" />);
    
    const button = screen.getByRole('button');
    
    await act(async () => {
      fireEvent.click(button);
    });

    expect(consoleSpy).toHaveBeenCalled();
    consoleSpy.mockRestore();
  });

  it('copies array data', async () => {
    const data = [1, 2, 3];
    render(<CopyButton data={data} />);

    const button = screen.getByRole('button');
    
    await act(async () => {
      fireEvent.click(button);
    });

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
      JSON.stringify(data, null, 2)
    );
  });
});
