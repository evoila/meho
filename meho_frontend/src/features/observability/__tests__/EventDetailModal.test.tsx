// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EventDetailModal Tests
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { EventDetailModal } from '../components/EventDetailModal';
import type { EventResponse } from '@/api/types';

// Mock createPortal
vi.mock('react-dom', async () => {
  const actual = await vi.importActual('react-dom');
  return {
    ...actual,
    createPortal: (node: React.ReactNode) => node,
  };
});

describe('EventDetailModal', () => {
  const mockLLMEvent: EventResponse = {
    id: 'event-1',
    timestamp: '2024-01-15T10:30:00Z',
    type: 'llm_call',
    summary: 'LLM reasoning step',
    details: {
      llm_prompt: 'You are a helpful assistant.',
      llm_response: 'I will help you with that.',
      token_usage: {
        prompt_tokens: 100,
        completion_tokens: 50,
        total_tokens: 150,
        estimated_cost_usd: 0.001,
      },
      model: 'gpt-4.1-mini',
    },
    duration_ms: 234,
    step_number: 1,
    agent_name: 'planner',
  };

  const mockHTTPEvent: EventResponse = {
    id: 'event-2',
    timestamp: '2024-01-15T10:30:01Z',
    type: 'http_request',
    summary: 'GET /api/vms',
    details: {
      http_method: 'GET',
      http_url: 'https://api.example.com/vms',
      http_status_code: 200,
      http_response_body: '{"vms": []}',
    },
    duration_ms: 345,
  };

  const mockOnClose = vi.fn();

  beforeEach(() => {
    mockOnClose.mockClear();
  });

  it('renders nothing when event is null', () => {
    const { container } = render(
      <EventDetailModal event={null} isOpen={true} onClose={mockOnClose} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when not open', () => {
    const { container } = render(
      <EventDetailModal event={mockLLMEvent} isOpen={false} onClose={mockOnClose} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders modal when open with event', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('LLM reasoning step')).toBeInTheDocument();
  });

  it('renders event type badge', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('llm_call')).toBeInTheDocument();
  });

  it('renders agent name when available', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('Agent: planner')).toBeInTheDocument();
  });

  it('renders step number when available', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('Step 1')).toBeInTheDocument();
  });

  it('renders LLM tab for LLM events', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('LLM')).toBeInTheDocument();
  });

  it('renders HTTP tab for HTTP events', () => {
    render(<EventDetailModal event={mockHTTPEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('HTTP')).toBeInTheDocument();
  });

  it('always renders Raw tab', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('Raw')).toBeInTheDocument();
  });

  it('renders token usage badge when available', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    // Multiple token badges may appear in header and content
    const tokenBadges = screen.getAllByText('150 tokens');
    expect(tokenBadges.length).toBeGreaterThan(0);
  });

  it('renders duration in footer', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    expect(screen.getByText('234ms')).toBeInTheDocument();
  });

  it('calls onClose when close button is clicked', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    
    // Find close button (has X icon)
    const closeButtons = screen.getAllByRole('button');
    const closeButton = closeButtons.find(btn => btn.querySelector('svg'));
    if (closeButton) {
      fireEvent.click(closeButton);
      expect(mockOnClose).toHaveBeenCalledTimes(1);
    }
  });

  it('calls onClose when backdrop is clicked', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    
    // Click backdrop (the outer div)
    const backdrop = document.querySelector('.fixed.inset-0');
    if (backdrop) {
      fireEvent.click(backdrop);
      expect(mockOnClose).toHaveBeenCalledTimes(1);
    }
  });

  it('does not close when modal content is clicked', () => {
    render(<EventDetailModal event={mockLLMEvent} isOpen={true} onClose={mockOnClose} />);
    
    // Click modal content
    const summary = screen.getByText('LLM reasoning step');
    fireEvent.click(summary);
    expect(mockOnClose).not.toHaveBeenCalled();
  });
});
