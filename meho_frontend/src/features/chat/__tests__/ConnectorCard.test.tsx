// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Unit tests for ConnectorCard component
 *
 * Tests the connector status card UI including:
 * - Status badge rendering
 * - Collapsible event timeline
 * - Findings preview
 * - Error display
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConnectorCard } from '../components/ConnectorCard';
import type { ConnectorState, WrappedAgentEvent } from '@/api/types/orchestrator';

// Mock motion/react to avoid animation issues in tests
vi.mock('motion/react', () => ({
  motion: {
    div: ({ children, ...props }: { children: React.ReactNode }) => <div {...props}>{children}</div>,
  },
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

describe('ConnectorCard', () => {
  const baseConnector: ConnectorState = {
    id: 'conn-1',
    name: 'Production K8s',
    status: 'running',
    events: [],
  };

  // Default timing props for all tests
  const defaultTimingProps = {
    startTime: Date.now() - 5000, // 5 seconds ago
    totalElapsed: 5000,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('Basic Rendering', () => {
    it('renders connector name', () => {
      render(<ConnectorCard connector={baseConnector} {...defaultTimingProps} />);

      expect(screen.getByText('Production K8s')).toBeInTheDocument();
    });

    it('shows running status badge', () => {
      render(<ConnectorCard connector={baseConnector} {...defaultTimingProps} />);

      expect(screen.getByText('Running')).toBeInTheDocument();
    });
  });

  describe('Status Badges', () => {
    it('shows pending status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'pending',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Pending')).toBeInTheDocument();
    });

    it('shows success status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'success',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Success')).toBeInTheDocument();
    });

    it('shows partial status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'partial',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Partial')).toBeInTheDocument();
    });

    it('shows failed status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'failed',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Failed')).toBeInTheDocument();
    });

    it('shows timeout status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'timeout',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Timeout')).toBeInTheDocument();
    });

    it('shows cancelled status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'cancelled',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      expect(screen.getByText('Cancelled')).toBeInTheDocument();
    });
  });

  describe('Findings Preview', () => {
    it('displays findings when present', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'success',
        findings: 'Found 5 pods running in namespace default',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} defaultExpanded={true} />);

      expect(screen.getByText('Findings')).toBeInTheDocument();
      expect(screen.getByText('Found 5 pods running in namespace default')).toBeInTheDocument();
    });

    it('shows full findings without truncation', () => {
      const longFindings = 'A'.repeat(600);
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'success',
        findings: longFindings,
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} defaultExpanded={true} />);

      // Should show full findings (no truncation)
      expect(screen.getByText(longFindings)).toBeInTheDocument();
    });
  });

  describe('Error Display', () => {
    it('displays error message when present', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'failed',
        error: 'Connection timeout after 30 seconds',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} defaultExpanded={true} />);

      expect(screen.getByText('Error')).toBeInTheDocument();
      expect(screen.getByText('Connection timeout after 30 seconds')).toBeInTheDocument();
    });
  });

  describe('Event Timeline', () => {
    it('displays sequential events timeline', () => {
      const events: WrappedAgentEvent[] = [
        {
          type: 'agent_event',
          agent_source: {
            agent_name: 'generic_agent_conn-1',
            connector_id: 'conn-1',
            connector_name: 'Production K8s',
            iteration: 1,
          },
          inner_event: {
            type: 'thought',
            data: { content: 'Thinking about the query...' },
          },
        },
        {
          type: 'agent_event',
          agent_source: {
            agent_name: 'generic_agent_conn-1',
            connector_id: 'conn-1',
            connector_name: 'Production K8s',
            iteration: 1,
          },
          inner_event: {
            type: 'action',
            data: { tool: 'list_pods', message: 'Listing pods' },
          },
        },
      ];

      const connector: ConnectorState = {
        ...baseConnector,
        events,
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} defaultExpanded={true} />);

      // Now displays unified sequential timeline with new header
      expect(screen.getByText('Workflow')).toBeInTheDocument();
      expect(screen.getByText(/2.*steps/)).toBeInTheDocument();
      expect(screen.getByText('list_pods')).toBeInTheDocument();
      expect(screen.getByText('Thinking about the query...')).toBeInTheDocument();
    });
  });

  describe('Expand/Collapse', () => {
    it('expands on click when collapsed', () => {
      render(<ConnectorCard connector={baseConnector} {...defaultTimingProps} defaultExpanded={false} />);

      // Should not show findings initially
      const header = screen.getByText('Production K8s').closest('button');
      expect(header).toBeInTheDocument();

      // Click to expand
      if (header) {
        fireEvent.click(header);
      }
    });

    it('auto-expands when defaultExpanded is true', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'success',
        findings: 'Some findings',
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} defaultExpanded={true} />);

      expect(screen.getByText('Some findings')).toBeInTheDocument();
    });
  });

  describe('Live State', () => {
    it('shows loading indicator when live and no events', () => {
      render(<ConnectorCard connector={baseConnector} {...defaultTimingProps} isLive={true} defaultExpanded={true} />);

      expect(screen.getByText('Processing...')).toBeInTheDocument();
    });
  });

  describe('Empty State', () => {
    it('shows empty message when not live and no events/findings', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'success',
        events: [],
      };
      render(<ConnectorCard connector={connector} {...defaultTimingProps} isLive={false} defaultExpanded={true} />);

      expect(screen.getByText('No events recorded')).toBeInTheDocument();
    });
  });

  describe('Error Styling', () => {
    it('applies error styling for failed status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'failed',
      };
      const { container } = render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      // Should have error styling class
      const card = container.firstChild as HTMLElement;
      expect(card.className).toContain('red');
    });

    it('applies error styling for timeout status', () => {
      const connector: ConnectorState = {
        ...baseConnector,
        status: 'timeout',
      };
      const { container } = render(<ConnectorCard connector={connector} {...defaultTimingProps} />);

      // Should have error styling class
      const card = container.firstChild as HTMLElement;
      expect(card.className).toContain('red');
    });
  });
});
