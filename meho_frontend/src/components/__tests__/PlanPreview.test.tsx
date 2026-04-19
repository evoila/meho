// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for PlanPreview component
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PlanPreview } from '../PlanPreview';
import type { Plan } from '../../lib/api-client';

describe('PlanPreview', () => {
  const mockPlan: Plan = {
    goal: 'Test goal',
    steps: [
      {
        id: 'step1',
        description: 'Search knowledge base',
        tool_name: 'search_knowledge',
        tool_args: { query: 'test' },
        depends_on: [],
      },
      {
        id: 'step2',
        description: 'Interpret results',
        tool_name: 'interpret_results',
        tool_args: { context: 'test' },
        depends_on: ['step1'],
      },
    ],
    notes: 'Test notes',
  };

  const mockHandlers = {
    onApprove: vi.fn(),
    onReject: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders plan goal', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    expect(screen.getByText('Test goal')).toBeInTheDocument();
  });

  it('renders all steps', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    expect(screen.getByText('Search knowledge base')).toBeInTheDocument();
    expect(screen.getByText('Interpret results')).toBeInTheDocument();
  });

  it('shows step count', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    // Component shows step count in the header as "2 steps"
    expect(screen.getByText(/2 steps/)).toBeInTheDocument();
  });

  it('calls onApprove when approve button clicked', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    const approveButton = screen.getByText('Approve & Execute');
    fireEvent.click(approveButton);

    expect(mockHandlers.onApprove).toHaveBeenCalledTimes(1);
  });

  it('calls onReject when reject button clicked', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    const rejectButton = screen.getByText('Reject');
    fireEvent.click(rejectButton);

    expect(mockHandlers.onReject).toHaveBeenCalledTimes(1);
  });

  it('disables buttons when approving', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
        isApproving={true}
      />
    );

    const approveButton = screen.getByText('Approving...');
    const rejectButton = screen.getByText('Reject');

    expect(approveButton).toBeDisabled();
    expect(rejectButton).toBeDisabled();
  });

  it('shows workflow ID', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-workflow-id-123"
        {...mockHandlers}
      />
    );

    expect(screen.getByText(/test-workflow-id-123/)).toBeInTheDocument();
  });

  it('handles plan with no steps', () => {
    const emptyPlan: Plan = {
      goal: 'Empty plan',
      steps: [],
      notes: 'No steps needed for this goal',
    };

    render(
      <PlanPreview
        plan={emptyPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    expect(screen.getByText('No steps needed for this goal')).toBeInTheDocument();
  });

  it('shows tool arguments in expandable section', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    const viewArgsButton = screen.getAllByText('View arguments')[0];
    expect(viewArgsButton).toBeInTheDocument();
  });

  it('shows dependencies when present', () => {
    render(
      <PlanPreview
        plan={mockPlan}
        workflowId="test-123"
        {...mockHandlers}
      />
    );

    // Component shows "Depends on:" label and step ids
    expect(screen.getByText('Depends on:')).toBeInTheDocument();
    expect(screen.getByText('step1')).toBeInTheDocument();
  });
});

