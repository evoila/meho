// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import { ArrowLeft, Settings, Upload, List, Key, Trash2, AlertTriangle, Globe, FileCode, Cpu, Boxes, Server, BookOpen, FileText, Brain, Webhook, Mail } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import type { UpdateConnectorRequest } from '../../api/types';
import { config } from '../../lib/config';
import { OpenAPISpecUpload } from './OpenAPISpecUpload';
import { EndpointBrowser } from './EndpointBrowser';
import ConnectorSettings from './ConnectorSettings';
import CredentialManagement from './CredentialManagement';
import ConnectionTest from './ConnectionTest';
import { SOAPWSDLPanel } from './SOAPWSDLPanel';
import { SOAPOperationsBrowser } from './SOAPOperationsBrowser';
import { SOAPTypesBrowser } from './SOAPTypesBrowser';
import { TypedOperationsBrowser } from './TypedOperationsBrowser';
import { TypedTypesBrowser } from './TypedTypesBrowser';
import { SkillEditor } from './SkillEditor';
import { ConnectorKnowledge } from './ConnectorKnowledge';
import { ConnectorMemory } from './ConnectorMemory';
import { ConnectorEvents } from './ConnectorEvents';
import { Modal } from '../../shared/components/ui';
import clsx from 'clsx';
import type { EmailDeliveryLogEntry } from '../../api/types/connector';

// ---------------------------------------------------------------------------
// EmailHistory - Inline component for Email History tab
// ---------------------------------------------------------------------------

function EmailHistory({ connectorId }: { connectorId: string }) {
  const apiClient = getAPIClient(config.apiURL);

  const { data: entries, isLoading } = useQuery({
    queryKey: ['email-history', connectorId],
    queryFn: () => apiClient.getEmailHistory(connectorId),
    refetchInterval: 30000, // Poll every 30s for new emails
  });

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-40">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
      </div>
    );
  }

  if (!entries || entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-40 text-text-secondary">
        <Mail className="h-10 w-10 mb-3 opacity-40" />
        <p className="text-sm">No emails sent yet</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-medium text-text-secondary">
          {entries.length} email{entries.length !== 1 ? 's' : ''} sent
        </h3>
      </div>
      {entries.map((entry: EmailDeliveryLogEntry) => {
        const date = new Date(entry.created_at);
        const formattedDate = date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        const formattedTime = date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

        return (
          <div
            key={entry.id}
            className="flex items-start gap-4 p-4 bg-white/5 rounded-xl border border-white/10 hover:border-white/20 transition-colors"
          >
            {/* Timestamp */}
            <div className="flex-shrink-0 text-right min-w-[80px]">
              <p className="text-xs font-medium text-text-secondary">{formattedDate}</p>
              <p className="text-xs text-text-tertiary">{formattedTime}</p>
            </div>

            {/* Details */}
            <div className="flex-1 min-w-0">
              <p className="text-sm font-medium text-white truncate">{entry.subject}</p>
              <p className="text-xs text-text-tertiary mt-1 truncate">
                To: {entry.to_emails.join(', ')}
              </p>
              {entry.error_message && (
                <p className="text-xs text-red-400 mt-1">{entry.error_message}</p>
              )}
            </div>

            {/* Status badge */}
            <span
              className={clsx(
                'flex-shrink-0 px-2.5 py-1 rounded-lg text-xs font-medium border',
                entry.status === 'sent' || entry.status === 'accepted'
                  ? 'bg-green-400/10 text-green-400 border-green-400/20'
                  : 'bg-red-400/10 text-red-400 border-red-400/20'
              )}
            >
              {entry.status === 'sent' ? 'Sent' : entry.status === 'accepted' ? 'Accepted' : 'Failed'}
            </span>
          </div>
        );
      })}
    </div>
  );
}

const CONNECTOR_BADGE_COLORS: Record<string, string> = {
  vmware: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
  proxmox: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
  gcp: 'bg-sky-500/10 text-sky-400 border-sky-500/20',
  kubernetes: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  prometheus: 'bg-red-500/10 text-red-400 border-red-500/20',
  loki: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  tempo: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
  alertmanager: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
  jira: 'bg-blue-600/10 text-blue-300 border-blue-600/20',
  confluence: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
  email: 'bg-green-500/10 text-green-400 border-green-500/20',
  argocd: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
  github: 'bg-violet-500/10 text-violet-400 border-violet-500/20',
  soap: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
  graphql: 'bg-pink-500/10 text-pink-400 border-pink-500/20',
  grpc: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
};

function connectorTypeBadgeColor(type: string): string {
  return CONNECTOR_BADGE_COLORS[type] || 'bg-sky-500/10 text-sky-400 border-sky-500/20';
}

// ---------------------------------------------------------------------------
// ConnectorDetails
// ---------------------------------------------------------------------------

interface ConnectorDetailsProps {
  connectorId: string;
  onBack: () => void;
}

type Tab = 'overview' | 'upload-spec' | 'endpoints' | 'types' | 'credentials' | 'skill' | 'knowledge' | 'memory' | 'events' | 'email-history';

export function ConnectorDetails({ connectorId, onBack }: ConnectorDetailsProps) { // NOSONAR (cognitive complexity)
  const [activeTab, setActiveTab] = useState<Tab>('endpoints');
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  const [skillDirty, setSkillDirty] = useState(false);
  const [showTabChangeConfirm, setShowTabChangeConfirm] = useState(false);
  const [pendingTab, setPendingTab] = useState<Tab | null>(null);
  const [memoryCount, setMemoryCount] = useState<number | null>(null);

  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  // Fetch connector details
  const { data: connector, isLoading } = useQuery({
    queryKey: ['connector', connectorId],
    queryFn: () => apiClient.getConnector(connectorId),
  });

  // Fetch all connectors (for related connectors selection)
  const { data: allConnectors } = useQuery({
    queryKey: ['connectors'],
    queryFn: () => apiClient.listConnectors(),
  });

  // Fetch credential status
  const { data: credentialStatus } = useQuery({
    queryKey: ['credentialStatus', connectorId],
    queryFn: () => apiClient.getCredentialStatus(connectorId),
    enabled: !!connector,
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: () => apiClient.deleteConnector(connectorId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['connectors'] });
      onBack();
    },
  });

  // Handler for updating connector
  const handleUpdateConnector = async (updates: UpdateConnectorRequest) => {
    await apiClient.updateConnector(connectorId, updates);
    // Invalidate query to refetch updated data
    await queryClient.invalidateQueries({ queryKey: ['connector', connectorId] });
  };

  // Handler for delete
  const handleDelete = () => {
    deleteMutation.mutate();
  };

  // Skill tab unsaved changes guards
  const handleTabChange = (tabId: Tab) => {
    if (skillDirty && activeTab === 'skill' && tabId !== 'skill') {
      setPendingTab(tabId);
      setShowTabChangeConfirm(true);
      return;
    }
    setActiveTab(tabId);
  };

  const handleBack = () => {
    if (skillDirty) {
      setPendingTab(null);
      setShowTabChangeConfirm(true);
      return;
    }
    onBack();
  };

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-primary"></div>
      </div>
    );
  }

  if (!connector) {
    return (
      <div className="flex flex-col items-center justify-center h-64 text-text-secondary">
        <AlertTriangle className="h-12 w-12 mb-4 opacity-50" />
        <p>Connector not found</p>
        <button
          onClick={onBack}
          className="mt-4 text-primary hover:text-primary-light transition-colors"
        >
          Go back
        </button>
      </div>
    );
  }

  // Dynamic tabs based on connector_type
  const connectorType = connector.connector_type || 'rest';
  const isSOAP = connectorType === 'soap';
  const isVMware = connectorType === 'vmware';
  const isProxmox = connectorType === 'proxmox';
  const isGCP = connectorType === 'gcp';
  const isKubernetes = connectorType === 'kubernetes';
  const isPrometheus = connectorType === 'prometheus';
  const isLoki = connectorType === 'loki';
  const isTempo = connectorType === 'tempo';
  const isAlertmanager = connectorType === 'alertmanager';
  const isJira = connectorType === 'jira';
  const isConfluence = connectorType === 'confluence';
  const isEmail = connectorType === 'email';
  const isArgoCD = connectorType === 'argocd';
  const isGitHub = connectorType === 'github';
  const isTypedConnector = isVMware || isProxmox || isGCP || isKubernetes || isPrometheus || isLoki || isTempo || isAlertmanager || isJira || isConfluence || isArgoCD || isGitHub;

  const tabs = isEmail
    ? [
        { id: 'endpoints' as const, label: 'Operations', icon: Cpu },
        { id: 'email-history' as const, label: 'Email History', icon: Mail },
        { id: 'overview' as const, label: 'Settings', icon: Settings },
        { id: 'skill' as const, label: 'Skill', icon: BookOpen },
        { id: 'knowledge' as const, label: 'Knowledge', icon: FileText },
        { id: 'memory' as const, label: memoryCount !== null ? `Memory (${memoryCount})` : 'Memory', icon: Brain },
        { id: 'events' as const, label: 'Events', icon: Webhook },
      ]
    : isTypedConnector
    ? [
        { id: 'endpoints' as const, label: 'Operations', icon: Cpu },
        { id: 'types' as const, label: 'Types', icon: Boxes },
        { id: 'overview' as const, label: 'Settings', icon: Settings },
        { id: 'credentials' as const, label: 'Credentials', icon: Key },
        { id: 'skill' as const, label: 'Skill', icon: BookOpen },
        { id: 'knowledge' as const, label: 'Knowledge', icon: FileText },
        { id: 'memory' as const, label: memoryCount !== null ? `Memory (${memoryCount})` : 'Memory', icon: Brain },
        { id: 'events' as const, label: 'Events', icon: Webhook },
      ]
    : isSOAP
    ? [
        { id: 'endpoints' as const, label: 'Operations', icon: List },
        { id: 'types' as const, label: 'Types', icon: Boxes },
        { id: 'upload-spec' as const, label: 'WSDL', icon: FileCode },
        { id: 'overview' as const, label: 'Settings', icon: Settings },
        { id: 'credentials' as const, label: 'Credentials', icon: Key },
        { id: 'skill' as const, label: 'Skill', icon: BookOpen },
        { id: 'knowledge' as const, label: 'Knowledge', icon: FileText },
        { id: 'memory' as const, label: memoryCount !== null ? `Memory (${memoryCount})` : 'Memory', icon: Brain },
        { id: 'events' as const, label: 'Events', icon: Webhook },
      ]
    : [
        { id: 'endpoints' as const, label: 'Endpoints', icon: List },
        { id: 'upload-spec' as const, label: 'Upload Spec', icon: Upload },
        { id: 'overview' as const, label: 'Settings', icon: Settings },
        { id: 'credentials' as const, label: 'Credentials', icon: Key },
        { id: 'skill' as const, label: 'Skill', icon: BookOpen },
        { id: 'knowledge' as const, label: 'Knowledge', icon: FileText },
        { id: 'memory' as const, label: memoryCount !== null ? `Memory (${memoryCount})` : 'Memory', icon: Brain },
        { id: 'events' as const, label: 'Events', icon: Webhook },
      ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button
          onClick={handleBack}
          className="p-2 hover:bg-white/5 rounded-xl transition-colors text-text-secondary hover:text-white"
        >
          <ArrowLeft className="h-5 w-5" />
        </button>
        <div className="flex-1">
          <h1 className="text-2xl font-bold text-white">{connector.name}</h1>
          <p className="text-text-secondary text-sm font-mono mt-1">{connector.base_url}</p>
        </div>
        <div className="flex items-center gap-3">
          {/* Connector type badge */}
          <span className={clsx(
            "flex items-center gap-1.5 px-3 py-1 rounded-lg text-xs font-medium border",
            connectorTypeBadgeColor(connectorType)
          )}>
            {isEmail ? <Mail className="h-3 w-3" /> : isTypedConnector ? <Server className="h-3 w-3" /> : connectorType === 'soap' ? <FileCode className="h-3 w-3" /> : <Globe className="h-3 w-3" />}
            {connectorType.toUpperCase()}
          </span>
          <span className={clsx(
            "px-3 py-1 rounded-lg text-xs font-medium border",
            connector.is_active
              ? "bg-green-500/10 text-green-400 border-green-500/20"
              : "bg-red-500/10 text-red-400 border-red-500/20"
          )}>
            {connector.is_active ? 'Active' : 'Inactive'}
          </span>
          <span className="px-3 py-1 bg-white/5 text-text-secondary border border-white/10 rounded-lg text-xs font-medium uppercase tracking-wider">
            {connector.auth_type}
          </span>
          <button
            onClick={() => setShowDeleteConfirm(true)}
            className="p-2 text-red-400 hover:bg-red-500/10 rounded-xl transition-colors border border-transparent hover:border-red-500/20"
            title="Delete connector"
          >
            <Trash2 className="h-4 w-4" />
          </button>
        </div>
      </div>

      {/* Tabs & Content */}
      <div className="glass rounded-2xl border border-white/10 overflow-hidden">
        <div className="border-b border-white/10 bg-white/5">
          <nav className="flex">
            {tabs.map((tab) => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => handleTabChange(tab.id as Tab)}
                  className={clsx(
                    "flex items-center gap-2 px-6 py-4 text-sm font-medium transition-all relative",
                    isActive ? "text-white" : "text-text-secondary hover:text-white hover:bg-white/5"
                  )}
                >
                  <Icon className={clsx("h-4 w-4", isActive ? "text-primary" : "opacity-70")} />
                  {tab.label}
                  {isActive && (
                    <motion.div
                      layoutId="activeTab"
                      className="absolute bottom-0 left-0 right-0 h-0.5 bg-gradient-to-r from-primary to-accent"
                    />
                  )}
                </button>
              );
            })}
          </nav>
        </div>

        <div className="p-6">
          <AnimatePresence mode="wait">
            <motion.div
              key={activeTab}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              transition={{ duration: 0.2 }}
            >
              {activeTab === 'overview' && (
                <ConnectorSettings
                  connector={connector}
                  onUpdate={handleUpdateConnector}
                  allConnectors={allConnectors || []}
                />
              )}

              {activeTab === 'upload-spec' && !isTypedConnector && (
                isSOAP ? (
                  <SOAPWSDLPanel
                    connectorId={connectorId}
                    connector={connector}
                    onSuccess={() => setActiveTab('endpoints')}
                  />
                ) : (
                  <OpenAPISpecUpload
                    connectorId={connectorId}
                    onSuccess={() => setActiveTab('endpoints')}
                  />
                )
              )}

              {activeTab === 'endpoints' && (
                isTypedConnector || isEmail ? (
                  <TypedOperationsBrowser connectorId={connectorId} connectorType={connectorType} />
                ) : isSOAP ? (
                  <SOAPOperationsBrowser connectorId={connectorId} />
                ) : (
                  <EndpointBrowser connectorId={connectorId} />
                )
              )}

              {activeTab === 'types' && (isSOAP || isTypedConnector) && (
                isTypedConnector ? (
                  <TypedTypesBrowser connectorId={connectorId} connectorType={connectorType} />
                ) : (
                  <SOAPTypesBrowser connectorId={connectorId} />
                )
              )}

              {activeTab === 'credentials' && (
                <div className="space-y-6">
                  <CredentialManagement
                    connector={connector}
                    apiClient={apiClient}
                  />

                  <ConnectionTest
                    connector={connector}
                    apiClient={apiClient}
                    credentialStatus={credentialStatus || null}
                  />
                </div>
              )}

              {activeTab === 'skill' && (
                <SkillEditor
                  connector={connector}
                  onDirtyChange={setSkillDirty}
                />
              )}

              {activeTab === 'knowledge' && (
                <ConnectorKnowledge connectorId={connectorId} connectorType={connectorType} />
              )}

              {activeTab === 'memory' && (
                <ConnectorMemory
                  connectorId={connectorId}
                  onCountChange={setMemoryCount}
                />
              )}

              {activeTab === 'events' && (
                <ConnectorEvents connectorId={connectorId} connectorType={connectorType} />
              )}

              {activeTab === 'email-history' && (
                <EmailHistory connectorId={connectorId} />
              )}
            </motion.div>
          </AnimatePresence>
        </div>
      </div>

      {/* Tab Change Confirmation Dialog (unsaved skill edits) */}
      <Modal
        isOpen={showTabChangeConfirm}
        onClose={() => setShowTabChangeConfirm(false)}
        title="Unsaved Changes"
        description="You have unsaved skill edits. Discard changes?"
        footer={
          <>
            <button
              onClick={() => setShowTabChangeConfirm(false)}
              className="px-4 py-2 text-sm text-text-secondary hover:text-white transition-colors"
            >
              Stay
            </button>
            <button
              onClick={() => {
                setSkillDirty(false);
                setShowTabChangeConfirm(false);
                if (pendingTab) {
                  setActiveTab(pendingTab);
                  setPendingTab(null);
                } else {
                  onBack();
                }
              }}
              className="px-4 py-2 text-sm bg-red-500/10 text-red-400 hover:bg-red-500/20 rounded-lg transition-colors"
            >
              Discard
            </button>
          </>
        }
      >
        <p className="text-sm text-text-secondary">
          Your unsaved skill edits will be lost if you navigate away.
        </p>
      </Modal>

      {/* Delete Confirmation Dialog */}
      <AnimatePresence>
        {showDeleteConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="absolute inset-0 bg-black/60 backdrop-blur-sm"
              onClick={() => setShowDeleteConfirm(false)}
            />
            <motion.div
              initial={{ opacity: 0, scale: 0.95, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.95, y: 20 }}
              className="relative w-full max-w-md glass rounded-2xl border border-white/10 p-6 shadow-2xl"
            >
              <div className="flex items-start gap-4">
                <div className="flex-shrink-0 w-12 h-12 rounded-full bg-red-500/10 flex items-center justify-center border border-red-500/20">
                  <AlertTriangle className="h-6 w-6 text-red-400" />
                </div>
                <div className="flex-1">
                  <h3 className="text-lg font-bold text-white mb-2">
                    Delete Connector
                  </h3>
                  <p className="text-text-secondary mb-4">
                    Are you sure you want to delete "<strong>{connector.name}</strong>"?
                  </p>
                  <div className="bg-red-500/5 border border-red-500/10 rounded-xl p-4 mb-6">
                    <p className="text-xs font-medium text-red-400 uppercase tracking-wider mb-2">
                      This will permanently delete:
                    </p>
                    <ul className="text-sm text-text-secondary space-y-1 list-disc list-inside">
                      <li>Connector configuration</li>
                      <li>OpenAPI specification files</li>
                      <li>Endpoint descriptors</li>
                      <li>Knowledge chunks</li>
                      <li>User credentials & tokens</li>
                    </ul>
                  </div>
                </div>
              </div>

              <div className="flex gap-3">
                <button
                  onClick={() => setShowDeleteConfirm(false)}
                  disabled={deleteMutation.isPending}
                  className="flex-1 px-4 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 rounded-xl text-white transition-all disabled:opacity-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDelete}
                  disabled={deleteMutation.isPending}
                  className="flex-1 px-4 py-2.5 bg-red-500 hover:bg-red-600 text-white rounded-xl transition-all disabled:opacity-50 flex items-center justify-center gap-2 shadow-lg shadow-red-500/20"
                >
                  {deleteMutation.isPending ? (
                    <>
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                      Deleting...
                    </>
                  ) : (
                    <>
                      <Trash2 className="h-4 w-4" />
                      Delete
                    </>
                  )}
                </button>
              </div>

              {deleteMutation.isError && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="mt-4 p-3 bg-red-500/10 border border-red-500/20 text-red-400 rounded-xl text-sm"
                >
                  Failed to delete connector: {(deleteMutation.error as Error).message}
                </motion.div>
              )}
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}
