// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * TokenUsageBadge Tests
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { TokenUsageBadge } from '../components/TokenUsageBadge';
import type { TokenUsage } from '@/api/types';

describe('TokenUsageBadge', () => {
  const mockUsage: TokenUsage = {
    prompt_tokens: 1500,
    completion_tokens: 500,
    total_tokens: 2000,
    estimated_cost_usd: 0.0060,
  };

  it('renders total token count', () => {
    render(<TokenUsageBadge usage={mockUsage} />);
    expect(screen.getByText('2,000 tokens')).toBeInTheDocument();
  });

  it('renders estimated cost when available', () => {
    render(<TokenUsageBadge usage={mockUsage} />);
    expect(screen.getByText('$0.0060')).toBeInTheDocument();
  });

  it('does not render cost when null', () => {
    const usageNoCost: TokenUsage = {
      ...mockUsage,
      estimated_cost_usd: null,
    };
    render(<TokenUsageBadge usage={usageNoCost} />);
    expect(screen.queryByText('$')).not.toBeInTheDocument();
  });

  it('expands to show breakdown on click', () => {
    render(<TokenUsageBadge usage={mockUsage} />);
    
    // Initially breakdown is hidden
    expect(screen.queryByText('Prompt:')).not.toBeInTheDocument();
    
    // Click to expand
    const button = screen.getByRole('button');
    fireEvent.click(button);
    
    // Now breakdown is visible
    expect(screen.getByText('Prompt:')).toBeInTheDocument();
    expect(screen.getByText('1,500')).toBeInTheDocument();
    expect(screen.getByText('Completion:')).toBeInTheDocument();
    expect(screen.getByText('500')).toBeInTheDocument();
  });

  it('starts expanded when defaultExpanded is true', () => {
    render(<TokenUsageBadge usage={mockUsage} defaultExpanded />);
    expect(screen.getByText('Prompt:')).toBeInTheDocument();
  });

  it('applies size variant correctly', () => {
    const { container } = render(<TokenUsageBadge usage={mockUsage} size="sm" />);
    expect(container.querySelector('.text-xs')).toBeInTheDocument();
  });

  it('applies custom className', () => {
    const { container } = render(<TokenUsageBadge usage={mockUsage} className="custom-class" />);
    expect(container.firstChild).toHaveClass('custom-class');
  });
});
