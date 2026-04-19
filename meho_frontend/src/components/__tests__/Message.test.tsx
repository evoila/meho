// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/* eslint-disable jsx-a11y/aria-role -- "role" is a component prop (user/assistant), not an ARIA role attribute */
/**
 * Tests for Message component
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Message, TypingIndicator } from '../Message';

describe('Message', () => {
  it('renders user message', () => {
    render(<Message role="user" content="Hello MEHO!" />);
    
    expect(screen.getByText('Hello MEHO!')).toBeInTheDocument();
  });

  it('renders assistant message', () => {
    render(<Message role="assistant" content="Hello! How can I help?" />);
    
    expect(screen.getByText('Hello! How can I help?')).toBeInTheDocument();
  });

  it('shows streaming indicator when streaming', () => {
    render(<Message role="assistant" content="Thinking..." isStreaming={true} />);
    
    // Component shows "Generating..." when streaming
    expect(screen.getByText('Generating...')).toBeInTheDocument();
  });

  it('renders multiline content', () => {
    const multilineContent = 'Line 1\nLine 2\nLine 3';
    render(<Message role="user" content={multilineContent} />);
    
    expect(screen.getByText(/Line 1/)).toBeInTheDocument();
  });

  it('applies correct styling for user messages', () => {
    const { container } = render(<Message role="user" content="Test" />);
    
    // User messages use bg-primary (from Tailwind theme)
    const messageDiv = container.querySelector('.bg-primary');
    expect(messageDiv).toBeInTheDocument();
  });

  it('applies correct styling for assistant messages', () => {
    const { container } = render(<Message role="assistant" content="Test" />);
    
    // Assistant messages use bg-surface
    const messageDiv = container.querySelector('.bg-surface');
    expect(messageDiv).toBeInTheDocument();
  });
});

describe('TypingIndicator', () => {
  it('renders typing indicator', () => {
    render(<TypingIndicator />);
    
    // Component shows "Thinking..."
    expect(screen.getByText('Thinking...')).toBeInTheDocument();
  });

  it('shows loading spinner', () => {
    const { container } = render(<TypingIndicator />);
    
    const spinner = container.querySelector('.animate-spin');
    expect(spinner).toBeInTheDocument();
  });
});

