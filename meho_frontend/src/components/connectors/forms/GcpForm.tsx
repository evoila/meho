// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server, Upload } from 'lucide-react';
import type { ConnectorFormBaseProps, GcpFormState } from './types';

export interface GcpFormProps extends ConnectorFormBaseProps {
  state: GcpFormState;
  onChange: (patch: Partial<GcpFormState>) => void;
  onAutoFill?: (fields: { name?: string }) => void;
}

export function validateGcpForm(state: GcpFormState): string | null {
  if (!state.projectId.trim()) return 'GCP Project ID is required';
  if (!state.serviceAccountJson.trim()) return 'Service Account JSON is required';
  return null;
}

export function GcpForm({ state, onChange, submitting, onAutoFill }: GcpFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-sky-500/5 rounded-xl border border-sky-500/20"
    >
      <div className="flex items-center gap-2 text-sky-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Google Cloud Platform Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="col-span-2 md:col-span-1">
          <label htmlFor="create-gcp-project-id" className="block text-sm font-medium text-text-secondary mb-2">
            GCP Project ID *
          </label>
          <input
            id="create-gcp-project-id"
            type="text"
            value={state.projectId}
            onChange={(e) => onChange({ projectId: e.target.value })}
            placeholder="my-gcp-project"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Your Google Cloud project identifier</p>
        </div>

        <div>
          <label htmlFor="create-gcp-default-region" className="block text-sm font-medium text-text-secondary mb-2">
            Default Region
          </label>
          <select
            id="create-gcp-default-region"
            value={state.defaultRegion}
            onChange={(e) => {
              onChange({ defaultRegion: e.target.value, defaultZone: e.target.value + '-a' });
            }}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all appearance-none"
          >
            <option value="us-central1">us-central1 (Iowa)</option>
            <option value="us-east1">us-east1 (South Carolina)</option>
            <option value="us-west1">us-west1 (Oregon)</option>
            <option value="europe-west1">europe-west1 (Belgium)</option>
            <option value="europe-west2">europe-west2 (London)</option>
            <option value="europe-west3">europe-west3 (Frankfurt)</option>
            <option value="asia-east1">asia-east1 (Taiwan)</option>
            <option value="asia-southeast1">asia-southeast1 (Singapore)</option>
          </select>
        </div>

        <div>
          <label htmlFor="create-gcp-default-zone" className="block text-sm font-medium text-text-secondary mb-2">
            Default Zone
          </label>
          <input
            id="create-gcp-default-zone"
            type="text"
            value={state.defaultZone}
            onChange={(e) => onChange({ defaultZone: e.target.value })}
            placeholder="us-central1-a"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Default zone for VMs and disks (e.g., us-central1-a)</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-gcp-service-account-json" className="block text-sm font-medium text-text-secondary mb-2">
            Service Account JSON *
          </label>
          <div className="space-y-3">
            <textarea
              id="create-gcp-service-account-json"
              value={state.serviceAccountJson}
              onChange={(e) => onChange({ serviceAccountJson: e.target.value })}
              placeholder={`{
  "type": "service_account",
  "project_id": "my-project",
  "private_key_id": "...",
  "private_key": "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n",
  "client_email": "my-sa@my-project.iam.gserviceaccount.com",
  ...
}`}
              rows={6}
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-sky-500/50 focus:border-sky-500/50 transition-all font-mono text-sm resize-none"
            />
            <div className="flex items-center gap-3">
              <label className="flex-1">
                <input
                  type="file"
                  accept=".json"
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    if (file) {
                      const reader = new FileReader();
                      reader.onload = (event) => {
                        const content = event.target?.result as string;
                        const patch: Partial<GcpFormState> = { serviceAccountJson: content };
                        try {
                          const parsed = JSON.parse(content);
                          if (parsed.project_id && !state.projectId) {
                            patch.projectId = parsed.project_id;
                          }
                          if (parsed.project_id) {
                            onAutoFill?.({ name: `GCP - ${parsed.project_id}` });
                          }
                        } catch {
                          // Ignore parse errors
                        }
                        onChange(patch);
                      };
                      reader.readAsText(file);
                    }
                  }}
                  disabled={submitting}
                  className="hidden"
                />
                <span className="flex items-center justify-center gap-2 px-4 py-2 bg-sky-500/10 hover:bg-sky-500/20 text-sky-400 rounded-lg cursor-pointer transition-colors text-sm font-medium">
                  <Upload className="h-4 w-4" />
                  Upload JSON Key File
                </span>
              </label>
              {state.serviceAccountJson && (
                <button
                  type="button"
                  onClick={() => onChange({ serviceAccountJson: '' })}
                  className="px-3 py-2 text-sm text-text-tertiary hover:text-red-400 transition-colors"
                >
                  Clear
                </button>
              )}
            </div>
          </div>
          <p className="text-xs text-text-tertiary mt-2">
            Paste the JSON key file content or upload the file.
            Create a Service Account in GCP Console with required permissions.
          </p>
        </div>
      </div>

      <div className="p-3 bg-sky-500/10 rounded-lg border border-sky-500/20 text-sky-300 text-sm">
        <p className="font-medium">☁️ Google Cloud Platform Connector</p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Create a Service Account in GCP Console with appropriate roles</li>
          <li>Required roles: Compute Viewer, Container Viewer, Monitoring Viewer</li>
          <li>Download the JSON key file and paste/upload it above</li>
          <li>MEHO will register 40+ GCP operations for: Compute Engine, GKE, Networking, Monitoring</li>
        </ol>
      </div>
    </motion.div>
  );
}
