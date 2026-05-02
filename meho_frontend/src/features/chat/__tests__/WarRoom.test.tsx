// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/* eslint-disable jsx-a11y/aria-role -- "role" is a component prop (user/assistant), not an ARIA role attribute */
/**
 * War Room Mode Frontend Tests (Phase 39)
 *
 * Tests sender name label rendering, consecutive sender collapsing,
 * and ChatInput processing state behavior.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Message } from '@/components/Message';
import { ChatInput } from '@/features/chat/components/ChatInput';

// Mock motion/react to avoid animation issues in tests
vi.mock('motion/react', () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode; [key: string]: unknown }) => {
      // Filter out framer-motion-specific props that are not valid DOM attributes
      const {
        initial: _initial, animate: _animate, transition: _transition, exit: _exit, whileHover: _whileHover, whileTap: _whileTap,
        variants: _variants, layout: _layout, layoutId: _layoutId, ...domProps
      } = props;
      return <div {...domProps}>{children}</div>;
    },
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

describe('Message Component - Sender Name Label', () => {
  it('renders sender name when showSenderName=true and senderName provided', () => {
    render(
      <Message
        role="user"
        content="Check pod logs"
        senderName="Alice"
        showSenderName={true}
      />
    );

    expect(screen.getByTestId('sender-name-label')).toHaveTextContent('Alice');
  });

  it('does NOT render sender name when showSenderName=false', () => {
    render(
      <Message
        role="user"
        content="Check pod logs"
        senderName="Alice"
        showSenderName={false}
      />
    );

    expect(screen.queryByTestId('sender-name-label')).toBeNull();
  });

  it('does NOT render sender name for assistant messages', () => {
    render(
      <Message
        role="assistant"
        content="Here are the pod logs..."
        senderName="MEHO"
        showSenderName={true}
      />
    );

    expect(screen.queryByTestId('sender-name-label')).toBeNull();
  });

  it('does NOT render sender name when senderName is undefined', () => {
    render(
      <Message
        role="user"
        content="Check pod logs"
        showSenderName={true}
      />
    );

    expect(screen.queryByTestId('sender-name-label')).toBeNull();
  });
});

describe('Consecutive Sender Collapsing', () => {
  it('shows sender name on first message, collapses on consecutive from same sender', () => {
    // Render two consecutive messages from Alice
    const { rerender } = render(
      <Message
        role="user"
        content="First message"
        senderName="Alice"
        showSenderName={true}
      />
    );

    // First message should show sender name
    expect(screen.getByTestId('sender-name-label')).toHaveTextContent('Alice');

    // Second message from same sender -- showSenderName=false (calculated by parent)
    rerender(
      <Message
        role="user"
        content="Second message"
        senderName="Alice"
        showSenderName={false}
      />
    );

    // Name label should not render for consecutive message
    expect(screen.queryByTestId('sender-name-label')).toBeNull();
  });

  it('shows sender name when different senders alternate', () => {
    // Alice's message -- first in run, should show name
    const { rerender } = render(
      <Message
        role="user"
        content="Alice message"
        senderName="Alice"
        showSenderName={true}
      />
    );

    expect(screen.getByTestId('sender-name-label')).toHaveTextContent('Alice');

    // Bob's message -- different sender, should show name
    rerender(
      <Message
        role="user"
        content="Bob message"
        senderName="Bob"
        showSenderName={true}
      />
    );

    expect(screen.getByTestId('sender-name-label')).toHaveTextContent('Bob');
  });
});

describe('ChatInput - War Room Processing State', () => {
  const defaultProps = {
    value: '',
    onChange: vi.fn(),
    onSend: vi.fn(),
    onStop: vi.fn(),
    isProcessing: false,
  };

  it('shows "MEHO is processing..." placeholder when isWarRoomProcessing=true', () => {
    render(<ChatInput {...defaultProps} isWarRoomProcessing={true} />);

    const textarea = screen.getByTestId('chat-input');
    expect(textarea).toHaveAttribute('placeholder', 'MEHO is processing...');
  });

  it('is disabled when isWarRoomProcessing=true', () => {
    render(<ChatInput {...defaultProps} isWarRoomProcessing={true} />);

    const textarea = screen.getByTestId('chat-input');
    expect(textarea).toBeDisabled();
  });

  it('is enabled when isWarRoomProcessing=false', () => {
    render(<ChatInput {...defaultProps} isWarRoomProcessing={false} />);

    const textarea = screen.getByTestId('chat-input');
    expect(textarea).not.toBeDisabled();
  });

  it('shows normal placeholder when isWarRoomProcessing=false', () => {
    render(<ChatInput {...defaultProps} isWarRoomProcessing={false} />);

    const textarea = screen.getByTestId('chat-input');
    expect(textarea).toHaveAttribute('placeholder', 'Ask MEHO anything...');
  });
});
