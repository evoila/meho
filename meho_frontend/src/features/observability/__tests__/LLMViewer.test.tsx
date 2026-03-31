// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * LLMViewer Tests
 */
import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { LLMViewer } from '../components/LLMViewer';
import type { EventDetails } from '@/api/types';

describe('LLMViewer', () => {
  const mockDetails: EventDetails = {
    llm_prompt: 'You are a helpful assistant.',
    llm_messages: [
      { role: 'user', content: 'What is the weather?' },
      { role: 'assistant', content: 'I cannot check the weather.' },
    ],
    llm_response: 'I cannot check the weather directly.',
    llm_parsed: { action: 'none', reason: 'no weather API' },
    token_usage: {
      prompt_tokens: 100,
      completion_tokens: 50,
      total_tokens: 150,
      estimated_cost_usd: 0.001,
    },
    llm_duration_ms: 234,
    model: 'gpt-4.1-mini',
  };

  it('renders model name', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('gpt-4.1-mini')).toBeInTheDocument();
  });

  it('renders duration', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('234ms')).toBeInTheDocument();
  });

  it('renders token usage badge', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('150 tokens')).toBeInTheDocument();
  });

  it('renders system prompt section', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('System Prompt')).toBeInTheDocument();
  });

  it('renders messages section with count', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('Messages (2)')).toBeInTheDocument();
  });

  it('renders response section', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('Response')).toBeInTheDocument();
  });

  it('renders parsed output section', () => {
    render(<LLMViewer details={mockDetails} />);
    expect(screen.getByText('Parsed Output')).toBeInTheDocument();
  });

  it('expands sections on click', () => {
    render(<LLMViewer details={mockDetails} />);
    
    // Find and click System Prompt section
    const systemPromptButton = screen.getByText('System Prompt');
    fireEvent.click(systemPromptButton);
    
    // Prompt content should be visible
    expect(screen.getByText('You are a helpful assistant.')).toBeInTheDocument();
  });

  it('handles minimal LLM details', () => {
    const minimalDetails: EventDetails = {
      llm_response: 'Just a response',
    };
    render(<LLMViewer details={minimalDetails} />);
    expect(screen.getByText('Response')).toBeInTheDocument();
    expect(screen.queryByText('System Prompt')).not.toBeInTheDocument();
  });
});
