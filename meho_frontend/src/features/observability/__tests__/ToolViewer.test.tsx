// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * ToolViewer Tests
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ToolViewer } from '../components/ToolViewer';
import type { EventDetails } from '@/api/types';

describe('ToolViewer', () => {
  const mockDetails: EventDetails = {
    tool_name: 'search_vms',
    tool_input: { filter: { name: 'prod-*' } },
    tool_output: { vms: [{ name: 'prod-vm-1' }], count: 1 },
    tool_duration_ms: 123,
  };

  it('renders tool name', () => {
    render(<ToolViewer details={mockDetails} />);
    expect(screen.getByText('search_vms')).toBeInTheDocument();
  });

  it('renders duration', () => {
    render(<ToolViewer details={mockDetails} />);
    expect(screen.getByText('123ms')).toBeInTheDocument();
  });

  it('renders Input section', () => {
    render(<ToolViewer details={mockDetails} />);
    expect(screen.getByText('Input')).toBeInTheDocument();
  });

  it('renders Output section', () => {
    render(<ToolViewer details={mockDetails} />);
    expect(screen.getByText('Output')).toBeInTheDocument();
  });

  it('displays error banner when tool_error is present', () => {
    const errorDetails: EventDetails = {
      ...mockDetails,
      tool_error: 'Connection timeout',
    };
    render(<ToolViewer details={errorDetails} />);
    expect(screen.getByText('Connection timeout')).toBeInTheDocument();
  });

  it('handles missing input gracefully', () => {
    const noInputDetails: EventDetails = {
      tool_name: 'get_status',
      tool_output: { status: 'ok' },
    };
    render(<ToolViewer details={noInputDetails} />);
    expect(screen.getByText('No input data')).toBeInTheDocument();
  });

  it('handles missing output gracefully', () => {
    const noOutputDetails: EventDetails = {
      tool_name: 'submit_request',
      tool_input: { request_id: '123' },
    };
    render(<ToolViewer details={noOutputDetails} />);
    expect(screen.getByText('No output data')).toBeInTheDocument();
  });

  it('displays fallback tool name when not provided', () => {
    const noNameDetails: EventDetails = {
      tool_input: { data: 'test' },
    };
    render(<ToolViewer details={noNameDetails} />);
    expect(screen.getByText('Tool Call')).toBeInTheDocument();
  });
});
