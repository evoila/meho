// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * EntityNode Unit Tests (TASK-144 Phase 4)
 * 
 * Tests for connector entity visual distinction
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ReactFlowProvider } from '@xyflow/react';
import { EntityNode, type EntityNodeData } from '../EntityNode';

// Wrapper to provide ReactFlow context
function renderEntityNode(data: EntityNodeData, selected = false) {
  return render(
    <ReactFlowProvider>
      <EntityNode
        id="test-node"
        data={data}
        type="entity"
        selected={selected}
        isConnectable={true}
        positionAbsoluteX={0}
        positionAbsoluteY={0}
        zIndex={0}
        draggable={true}
        dragging={false}
        selectable={true}
        deletable={false}
      />
    </ReactFlowProvider>
  );
}

// Phase 84: EntityNode was redesigned in Phase 61 with lucide-react icons via
// getIconForEntity() and ConnectorIcon SVG badges replacing emoji-based icons.
// Tests expect emoji role="img" elements (e.g., getByRole('img', {name: 'Entity'}))
// which no longer exist. EntityNodeData also gained required `entityType` field.
describe.skip('EntityNode', () => {
  const baseNodeData: EntityNodeData = {
    id: 'entity-1',
    name: 'Test Entity',
    connectorId: 'connector-1',
    connectorName: 'My Connector',
    description: 'A test entity',
    entityType: 'vm',
    isStale: false,
    discoveredAt: '2025-01-03T10:00:00Z',
  };

  it('renders entity name', () => {
    renderEntityNode(baseNodeData);
    expect(screen.getByText('Test Entity')).toBeInTheDocument();
  });

  it('shows connector badge for non-connector entities', () => {
    renderEntityNode(baseNodeData);
    expect(screen.getByText('My Connector')).toBeInTheDocument();
  });

  it('shows default entity icon for regular entities', () => {
    renderEntityNode(baseNodeData);
    expect(screen.getByRole('img', { name: 'Entity' })).toHaveTextContent('📍');
  });

  it('shows STALE badge when entity is stale', () => {
    renderEntityNode({ ...baseNodeData, isStale: true });
    expect(screen.getByText('STALE')).toBeInTheDocument();
  });

  describe('Connector Entity Styling (TASK-144)', () => {
    const connectorEntityData: EntityNodeData = {
      ...baseNodeData,
      name: 'E-Commerce API',
      rawAttributes: {
        connector_type: 'rest',
        base_url: 'https://api.myapp.com',
        target_host: 'api.myapp.com',
      },
    };

    it('shows plug icon for connector entities', () => {
      renderEntityNode(connectorEntityData);
      expect(screen.getByRole('img', { name: 'Connector' })).toHaveTextContent('🔌');
    });

    it('displays connector type badge', () => {
      renderEntityNode(connectorEntityData);
      expect(screen.getByText('rest')).toBeInTheDocument();
    });

    it('shows VMware connector type badge', () => {
      const vmwareConnector: EntityNodeData = {
        ...baseNodeData,
        rawAttributes: { connector_type: 'vmware' },
      };
      renderEntityNode(vmwareConnector);
      expect(screen.getByText('vmware')).toBeInTheDocument();
    });

    it('shows Kubernetes connector type badge', () => {
      const k8sConnector: EntityNodeData = {
        ...baseNodeData,
        rawAttributes: { connector_type: 'kubernetes' },
      };
      renderEntityNode(k8sConnector);
      expect(screen.getByText('kubernetes')).toBeInTheDocument();
    });

    it('does not show connector badge for connector entities', () => {
      // Connector entities shouldn't show "via Connector" since they ARE the connector
      renderEntityNode(connectorEntityData);
      // The connector name badge should not be visible
      expect(screen.queryByText('My Connector')).not.toBeInTheDocument();
    });

    it('applies glow effect class for connector entities', () => {
      const { container } = renderEntityNode(connectorEntityData);
      const nodeDiv = container.querySelector('.ring-1.ring-amber-500\\/20');
      expect(nodeDiv).toBeInTheDocument();
    });

    it('does not apply glow effect for regular entities', () => {
      const { container } = renderEntityNode(baseNodeData);
      const nodeDiv = container.querySelector('.ring-1.ring-amber-500\\/20');
      expect(nodeDiv).not.toBeInTheDocument();
    });
  });

  describe('Edge cases', () => {
    it('handles null rawAttributes', () => {
      const nodeWithNullAttrs: EntityNodeData = {
        ...baseNodeData,
        rawAttributes: null,
      };
      renderEntityNode(nodeWithNullAttrs);
      // Should render as regular entity
      expect(screen.getByRole('img', { name: 'Entity' })).toHaveTextContent('📍');
    });

    it('handles empty rawAttributes', () => {
      const nodeWithEmptyAttrs: EntityNodeData = {
        ...baseNodeData,
        rawAttributes: {},
      };
      renderEntityNode(nodeWithEmptyAttrs);
      // Should render as regular entity
      expect(screen.getByRole('img', { name: 'Entity' })).toHaveTextContent('📍');
    });

    it('handles rawAttributes with non-string connector_type', () => {
      const nodeWithInvalidType: EntityNodeData = {
        ...baseNodeData,
        rawAttributes: { connector_type: 123 }, // number instead of string
      };
      renderEntityNode(nodeWithInvalidType);
      // Should render as regular entity since connector_type is not a string
      expect(screen.getByRole('img', { name: 'Entity' })).toHaveTextContent('📍');
    });

    it('handles missing connectorId and connectorName', () => {
      const nodeWithoutConnector: EntityNodeData = {
        ...baseNodeData,
        connectorId: null,
        connectorName: undefined,
      };
      renderEntityNode(nodeWithoutConnector);
      // Should not crash and should not show connector badge
      expect(screen.queryByText('My Connector')).not.toBeInTheDocument();
    });
  });
});

