// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { FileCode, Download, Loader2 } from 'lucide-react';
import { motion } from 'motion/react';
import { getAPIClient } from '../../lib/api-client';
import { config } from '../../lib/config';
import clsx from 'clsx';

interface SOAPWSDLPanelProps {
  connectorId: string;
  connector: import('../../lib/api-client').Connector;
  onSuccess: () => void;
}

export function SOAPWSDLPanel({ connectorId, connector, onSuccess }: SOAPWSDLPanelProps) {
  const [wsdlUrl, setWsdlUrl] = useState(
    (connector.protocol_config && 'wsdl_url' in connector.protocol_config
      ? String(connector.protocol_config.wsdl_url ?? '')
      : '')
  );
  const [isIngesting, setIsIngesting] = useState(false);
  const [result, setResult] = useState<{ success: boolean; message: string; operations?: number; types?: number } | null>(null);
  
  const apiClient = getAPIClient(config.apiURL);
  const queryClient = useQueryClient();

  const handleIngestWSDL = async () => {
    setIsIngesting(true);
    setResult(null);
    try {
      const response = await apiClient.ingestWSDL(connectorId, wsdlUrl || undefined);
      setResult({
        success: true,
        message: `Successfully discovered ${response.operations_count} operations and ${response.types_count} type definitions from ${response.services.length} service(s)`,
        operations: response.operations_count,
        types: response.types_count,
      });
      // Invalidate queries to refresh data
      queryClient.invalidateQueries({ queryKey: ['soap-operations', connectorId] });
      queryClient.invalidateQueries({ queryKey: ['soap-types', connectorId] });
      onSuccess();
    } catch (err: unknown) {
      setResult({
        success: false,
        message: err instanceof Error ? err.message : 'Failed to ingest WSDL',
      });
    } finally {
      setIsIngesting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-white">
        <FileCode className="h-5 w-5 text-amber-400" />
        <h3 className="text-lg font-medium">WSDL Configuration</h3>
      </div>

      <div className="space-y-4">
        <div>
          <label htmlFor="soap-wsdl-url" className="block text-sm font-medium text-text-secondary mb-2">
            WSDL URL
          </label>
          <input
            id="soap-wsdl-url"
            type="url"
            value={wsdlUrl}
            onChange={(e) => setWsdlUrl(e.target.value)}
            placeholder="https://vcenter.local/sdk/vimService.wsdl"
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-amber-500/50 focus:border-amber-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">
            Leave empty to use the WSDL URL from connector configuration
          </p>
        </div>

        <button
          onClick={handleIngestWSDL}
          disabled={isIngesting}
          className="flex items-center gap-2 px-6 py-3 bg-gradient-to-r from-amber-500 to-amber-600 hover:shadow-lg hover:shadow-amber-500/25 text-white rounded-xl font-medium transition-all disabled:opacity-50"
        >
          {isIngesting ? (
            <>
              <Loader2 className="h-5 w-5 animate-spin" />
              Parsing WSDL...
            </>
          ) : (
            <>
              <Download className="h-5 w-5" />
              Ingest WSDL
            </>
          )}
        </button>

        {result && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            className={clsx(
              "p-4 rounded-xl border",
              result.success
                ? "bg-green-500/10 border-green-500/20 text-green-400"
                : "bg-red-500/10 border-red-500/20 text-red-400"
            )}
          >
            <p className="font-medium">{result.success ? '✅ Success' : '❌ Error'}</p>
            <p className="text-sm mt-1 opacity-80">{result.message}</p>
          </motion.div>
        )}
      </div>

      <div className="p-4 bg-amber-500/5 rounded-xl border border-amber-500/10">
        <h4 className="text-sm font-medium text-amber-400 mb-2">About SOAP/WSDL</h4>
        <p className="text-sm text-text-secondary">
          WSDL (Web Services Description Language) describes the operations available in a SOAP service.
          When you ingest a WSDL, MEHO parses it and discovers all available operations, making them
          accessible to the AI agent via natural language.
        </p>
      </div>
    </div>
  );
}

