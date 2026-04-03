// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * HTTPViewer Tests
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { HTTPViewer } from '../components/HTTPViewer';
import type { EventDetails } from '@/api/types';

describe('HTTPViewer', () => {
  const mockDetails: EventDetails = {
    http_method: 'GET',
    http_url: 'https://api.example.com/vms',
    http_status_code: 200,
    http_headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer xxx',
    },
    http_request_body: '{"filter": "active"}',
    http_response_body: '{"vms": [{"name": "vm-1"}]}',
    http_duration_ms: 345,
  };

  it('renders HTTP method', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('GET')).toBeInTheDocument();
  });

  it('renders URL', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('https://api.example.com/vms')).toBeInTheDocument();
  });

  it('renders status badge', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('200')).toBeInTheDocument();
  });

  it('renders duration', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('345ms')).toBeInTheDocument();
  });

  it('renders request headers section', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('Request Headers')).toBeInTheDocument();
  });

  it('renders request body section', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('Request Body')).toBeInTheDocument();
  });

  it('renders response body section', () => {
    render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('Response Body')).toBeInTheDocument();
  });

  it('applies correct color for different HTTP methods', () => {
    const { rerender } = render(<HTTPViewer details={mockDetails} />);
    expect(screen.getByText('GET')).toHaveClass('text-emerald-400');
    
    rerender(<HTTPViewer details={{ ...mockDetails, http_method: 'POST' }} />);
    expect(screen.getByText('POST')).toHaveClass('text-amber-400');
    
    rerender(<HTTPViewer details={{ ...mockDetails, http_method: 'DELETE' }} />);
    expect(screen.getByText('DELETE')).toHaveClass('text-red-400');
  });

  it('handles error status codes', () => {
    render(<HTTPViewer details={{ ...mockDetails, http_status_code: 500 }} />);
    const badge = screen.getByText('500');
    expect(badge).toBeInTheDocument();
  });

  it('handles missing optional fields', () => {
    const minimalDetails: EventDetails = {
      http_method: 'GET',
      http_url: 'https://api.example.com/test',
    };
    render(<HTTPViewer details={minimalDetails} />);
    expect(screen.getByText('GET')).toBeInTheDocument();
    expect(screen.queryByText('Request Headers')).not.toBeInTheDocument();
  });
});
