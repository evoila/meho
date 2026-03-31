// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Connectors Page - Manage API connectors and endpoints
 *
 * Features:
 * - List all connectors
 * - Create/edit connectors
 * - Upload OpenAPI specs
 * - Browse and edit endpoints
 * - Test API calls
 * - Configure credentials
 * - Contextual audit history section (admin-only, collapsed by default)
 */
import { useState } from 'react';
import { ChevronDown, ChevronRight, ClipboardList } from 'lucide-react';
import { ConnectorList } from '../components/connectors/ConnectorList';
import { ConnectorDetails } from '../components/connectors/ConnectorDetails';
import { AuditTable } from '../features/audit';
import { useAuth } from '../contexts/AuthContext';
import { motion, AnimatePresence } from 'motion/react';

export function ConnectorsPage() {
  const [selectedConnectorId, setSelectedConnectorId] = useState<string | null>(null);
  const [auditExpanded, setAuditExpanded] = useState(false);
  const { user } = useAuth();
  const isAdmin = !!user?.isGlobalAdmin;

  return (
    <div className="flex flex-col h-screen bg-background relative overflow-hidden">
      {/* Background Effects */}
      <div className="absolute inset-0 pointer-events-none">
        <div className="absolute top-0 right-0 w-[500px] h-[500px] bg-primary/5 rounded-full blur-[100px]" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-secondary/5 rounded-full blur-[100px]" />
      </div>

      <div className="flex-1 overflow-y-auto z-10">
        <div className="max-w-7xl mx-auto p-6 lg:p-8">
          <AnimatePresence mode="wait">
            {!selectedConnectorId ? (
              <motion.div
                key="list"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                transition={{ duration: 0.2 }}
              >
                <ConnectorList onSelectConnector={setSelectedConnectorId} />

                {/* Contextual audit section -- admin only, default collapsed */}
                {isAdmin && (
                  <div className="mt-8">
                    <button
                      onClick={() => setAuditExpanded(!auditExpanded)}
                      className="flex items-center gap-2 text-sm font-medium text-text-secondary hover:text-white transition-colors"
                    >
                      {auditExpanded ? (
                        <ChevronDown className="h-4 w-4" />
                      ) : (
                        <ChevronRight className="h-4 w-4" />
                      )}
                      <ClipboardList className="h-4 w-4" />
                      Recent Connector Activity
                    </button>
                    {auditExpanded && (
                      <div className="mt-3">
                        <AuditTable
                          defaultFilters={{ resource_type: 'connector' }}
                          limit={10}
                        />
                      </div>
                    )}
                  </div>
                )}
              </motion.div>
            ) : (
              <motion.div
                key="details"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                transition={{ duration: 0.2 }}
              >
                <ConnectorDetails
                  connectorId={selectedConnectorId}
                  onBack={() => setSelectedConnectorId(null)}
                />
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
}
