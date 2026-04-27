// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Server } from 'lucide-react';
import type { ConnectorFormBaseProps, AwsFormState } from './types';

export interface AwsFormProps extends ConnectorFormBaseProps {
  state: AwsFormState;
  onChange: (patch: Partial<AwsFormState>) => void;
}

export function validateAwsForm(state: AwsFormState): string | null {
  if (!state.defaultRegion.trim()) return 'Default region is required';
  if (!!state.accessKeyId.trim() !== !!state.secretAccessKey.trim()) {
    return 'Access Key ID and Secret Access Key must both be provided or both left empty';
  }
  return null;
}

export function AwsForm({ state, onChange, submitting }: AwsFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-orange-500/5 rounded-xl border border-orange-500/20"
    >
      <div className="flex items-center gap-2 text-orange-400 text-sm font-medium">
        <Server className="h-4 w-4" />
        Amazon Web Services Configuration
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label htmlFor="create-aws-access-key-id" className="block text-sm font-medium text-text-secondary mb-2">
            Access Key ID
          </label>
          <input
            id="create-aws-access-key-id"
            type="text"
            value={state.accessKeyId}
            onChange={(e) => onChange({ accessKeyId: e.target.value })}
            placeholder="AKIAIOSFODNN7EXAMPLE"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Leave blank to use IAM role or environment credentials</p>
        </div>

        <div>
          <label htmlFor="create-aws-secret-access-key" className="block text-sm font-medium text-text-secondary mb-2">
            Secret Access Key
          </label>
          <input
            id="create-aws-secret-access-key"
            type="password"
            value={state.secretAccessKey}
            onChange={(e) => onChange({ secretAccessKey: e.target.value })}
            placeholder="AWS secret access key"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Leave blank to use IAM role or environment credentials</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-aws-default-region" className="block text-sm font-medium text-text-secondary mb-2">
            Default Region
          </label>
          <input
            id="create-aws-default-region"
            type="text"
            value={state.defaultRegion}
            onChange={(e) => onChange({ defaultRegion: e.target.value })}
            placeholder="us-east-1"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-orange-500/50 focus:border-orange-500/50 transition-all"
          />
          <p className="text-xs text-text-tertiary mt-1">Default AWS region for API calls (e.g., us-east-1, eu-west-1)</p>
        </div>
      </div>

      <div className="text-xs text-text-tertiary space-y-1 mt-2">
        <p className="font-medium text-text-secondary">Setup instructions:</p>
        <ol className="list-decimal list-inside space-y-0.5 ml-1">
          <li>Create an IAM user in AWS Console with programmatic access</li>
          <li>Attach ReadOnlyAccess policy or specific service policies (EC2, ECS, EKS, S3, Lambda, RDS, CloudWatch, VPC)</li>
          <li>Copy the Access Key ID and Secret Access Key</li>
          <li>MEHO will register 25 AWS operations for: EC2, ECS, EKS, S3, Lambda, RDS, CloudWatch, VPC</li>
        </ol>
      </div>
    </motion.div>
  );
}
