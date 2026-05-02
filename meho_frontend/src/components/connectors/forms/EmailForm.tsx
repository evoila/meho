// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { motion } from 'motion/react';
import { Mail } from 'lucide-react';
import type { ConnectorFormBaseProps, EmailFormState } from './types';
import type { EmailProviderType } from '../../../api/types/connector';

export interface EmailFormProps extends ConnectorFormBaseProps {
  state: EmailFormState;
  onChange: (patch: Partial<EmailFormState>) => void;
}

export function validateEmailForm(state: EmailFormState): string | null {
  if (!state.fromEmail.trim()) return 'From email is required';
  if (!state.defaultRecipients.trim()) return 'Default recipients are required';
  if (state.providerType === 'smtp' && !state.smtpHost.trim()) return 'SMTP host is required';
  if (state.providerType === 'sendgrid' && !state.sendgridApiKey.trim()) return 'SendGrid API key is required';
  if (state.providerType === 'mailgun') {
    if (!state.mailgunApiKey.trim()) return 'Mailgun API key is required';
    if (!state.mailgunDomain.trim()) return 'Mailgun domain is required';
  }
  if (state.providerType === 'ses') {
    if (!state.sesAccessKey.trim()) return 'SES access key is required';
    if (!state.sesSecretKey.trim()) return 'SES secret key is required';
  }
  if (state.providerType === 'generic_http') {
    if (!state.httpEndpointUrl.trim()) return 'HTTP endpoint URL is required';
    if (!state.httpPayloadTemplate.trim()) return 'HTTP payload template is required';
  }
  return null;
}

export function EmailForm({ state, onChange, submitting }: EmailFormProps) {
  return (
    <motion.div
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: 'auto' }}
      className="space-y-6 p-6 bg-green-500/5 rounded-xl border border-[#22C55E]/30"
    >
      <div className="flex items-center gap-2 text-[#22C55E] text-sm font-medium">
        <Mail className="h-4 w-4" />
        Email Connector Configuration
      </div>

      {/* Common fields */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div>
          <label htmlFor="create-email-from" className="block text-sm font-medium text-text-secondary mb-2">
            From Email *
          </label>
          <input
            id="create-email-from"
            type="email"
            value={state.fromEmail}
            onChange={(e) => onChange({ fromEmail: e.target.value })}
            placeholder="meho@company.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
          />
        </div>

        <div>
          <label htmlFor="create-email-from-name" className="block text-sm font-medium text-text-secondary mb-2">
            From Name
          </label>
          <input
            id="create-email-from-name"
            type="text"
            value={state.fromName}
            onChange={(e) => onChange({ fromName: e.target.value })}
            placeholder="MEHO Alerts"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
          />
        </div>

        <div className="col-span-2">
          <label htmlFor="create-email-recipients" className="block text-sm font-medium text-text-secondary mb-2">
            Default Recipients *
          </label>
          <input
            id="create-email-recipients"
            type="text"
            value={state.defaultRecipients}
            onChange={(e) => onChange({ defaultRecipients: e.target.value })}
            placeholder="ops-team@company.com, sre@company.com"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
          />
          <p className="text-xs text-text-tertiary mt-1">Comma-separated email addresses</p>
        </div>

        <div className="col-span-2">
          <label htmlFor="create-email-routing-desc" className="block text-sm font-medium text-text-secondary mb-2">
            Routing Description
          </label>
          <textarea
            id="create-email-routing-desc"
            value={state.routingDescription}
            onChange={(e) => onChange({ routingDescription: e.target.value })}
            placeholder="Email connector for sending investigation reports"
            rows={2}
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm resize-none"
          />
          <p className="text-xs text-text-tertiary mt-1">Helps the orchestrator decide when to send email notifications</p>
        </div>
      </div>

      {/* Provider selector */}
      <div>
        <label htmlFor="create-email-provider" className="block text-sm font-medium text-text-secondary mb-2">
          Email Provider *
        </label>
        <select
          id="create-email-provider"
          value={state.providerType}
          onChange={(e) => {
            onChange({
              providerType: e.target.value as EmailProviderType,
              smtpHost: '', smtpPort: 587, smtpTls: true,
              smtpUsername: '', smtpPassword: '',
              sendgridApiKey: '',
              mailgunApiKey: '', mailgunDomain: '',
              sesAccessKey: '', sesSecretKey: '', sesRegion: 'us-east-1',
              httpEndpointUrl: '', httpAuthHeader: '', httpPayloadTemplate: '',
            });
          }}
          disabled={submitting}
          className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all appearance-none text-sm"
        >
          <option value="smtp">SMTP</option>
          <option value="sendgrid">SendGrid</option>
          <option value="mailgun">Mailgun</option>
          <option value="ses">Amazon SES</option>
          <option value="generic_http">Generic HTTP</option>
        </select>
      </div>

      {/* SMTP fields */}
      {state.providerType === 'smtp' && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
          <div>
            <label htmlFor="create-smtp-host" className="block text-sm font-medium text-text-secondary mb-2">
              SMTP Host *
            </label>
            <input
              id="create-smtp-host"
              type="text"
              value={state.smtpHost}
              onChange={(e) => onChange({ smtpHost: e.target.value })}
              placeholder="smtp.gmail.com"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="create-smtp-port" className="block text-sm font-medium text-text-secondary mb-2">
                Port
              </label>
              <input
                id="create-smtp-port"
                type="number"
                value={state.smtpPort}
                onChange={(e) => onChange({ smtpPort: parseInt(e.target.value) || 587 })}
                disabled={submitting}
                className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
              />
            </div>
            <div className="flex items-end">
              <label className="flex items-center gap-2 cursor-pointer p-3">
                <input
                  type="checkbox"
                  checked={state.smtpTls}
                  onChange={(e) => onChange({ smtpTls: e.target.checked })}
                  disabled={submitting}
                  className="rounded border-white/20 bg-surface text-[#22C55E] focus:ring-[#22C55E]/50"
                />
                <span className="text-sm text-text-secondary">TLS</span>
              </label>
            </div>
          </div>
          <div>
            <label htmlFor="create-smtp-username" className="block text-sm font-medium text-text-secondary mb-2">
              Username
            </label>
            <input
              id="create-smtp-username"
              type="text"
              value={state.smtpUsername}
              onChange={(e) => onChange({ smtpUsername: e.target.value })}
              placeholder="user@gmail.com"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-smtp-password" className="block text-sm font-medium text-text-secondary mb-2">
              Password
            </label>
            <input
              id="create-smtp-password"
              type="password"
              value={state.smtpPassword}
              onChange={(e) => onChange({ smtpPassword: e.target.value })}
              placeholder="App password"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
        </div>
      )}

      {/* SendGrid fields */}
      {state.providerType === 'sendgrid' && (
        <div className="p-4 bg-white/5 rounded-xl border border-white/10">
          <label htmlFor="create-sendgrid-api-key" className="block text-sm font-medium text-text-secondary mb-2">
            SendGrid API Key *
          </label>
          <input
            id="create-sendgrid-api-key"
            type="password"
            value={state.sendgridApiKey}
            onChange={(e) => onChange({ sendgridApiKey: e.target.value })}
            placeholder="SG.xxxxx"
            disabled={submitting}
            className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
          />
          <p className="text-xs text-text-tertiary mt-1">Create at app.sendgrid.com/settings/api_keys</p>
        </div>
      )}

      {/* Mailgun fields */}
      {state.providerType === 'mailgun' && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
          <div>
            <label htmlFor="create-mailgun-api-key" className="block text-sm font-medium text-text-secondary mb-2">
              Mailgun API Key *
            </label>
            <input
              id="create-mailgun-api-key"
              type="password"
              value={state.mailgunApiKey}
              onChange={(e) => onChange({ mailgunApiKey: e.target.value })}
              placeholder="key-xxxxx"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-mailgun-domain" className="block text-sm font-medium text-text-secondary mb-2">
              Mailgun Domain *
            </label>
            <input
              id="create-mailgun-domain"
              type="text"
              value={state.mailgunDomain}
              onChange={(e) => onChange({ mailgunDomain: e.target.value })}
              placeholder="mg.company.com"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
        </div>
      )}

      {/* SES fields */}
      {state.providerType === 'ses' && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
          <div>
            <label htmlFor="create-ses-access-key" className="block text-sm font-medium text-text-secondary mb-2">
              Access Key ID *
            </label>
            <input
              id="create-ses-access-key"
              type="text"
              value={state.sesAccessKey}
              onChange={(e) => onChange({ sesAccessKey: e.target.value })}
              placeholder="AKIAIOSFODNN7EXAMPLE"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-ses-secret-key" className="block text-sm font-medium text-text-secondary mb-2">
              Secret Access Key *
            </label>
            <input
              id="create-ses-secret-key"
              type="password"
              value={state.sesSecretKey}
              onChange={(e) => onChange({ sesSecretKey: e.target.value })}
              placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-ses-region" className="block text-sm font-medium text-text-secondary mb-2">
              SES Region
            </label>
            <select
              id="create-ses-region"
              value={state.sesRegion}
              onChange={(e) => onChange({ sesRegion: e.target.value })}
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all appearance-none text-sm"
            >
              <option value="us-east-1">US East (N. Virginia)</option>
              <option value="us-east-2">US East (Ohio)</option>
              <option value="us-west-2">US West (Oregon)</option>
              <option value="eu-west-1">EU (Ireland)</option>
              <option value="eu-west-2">EU (London)</option>
              <option value="eu-central-1">EU (Frankfurt)</option>
              <option value="ap-southeast-1">Asia Pacific (Singapore)</option>
              <option value="ap-southeast-2">Asia Pacific (Sydney)</option>
              <option value="ap-northeast-1">Asia Pacific (Tokyo)</option>
            </select>
          </div>
        </div>
      )}

      {/* Generic HTTP fields */}
      {state.providerType === 'generic_http' && (
        <div className="grid grid-cols-1 gap-6 p-4 bg-white/5 rounded-xl border border-white/10">
          <div>
            <label htmlFor="create-http-endpoint-url" className="block text-sm font-medium text-text-secondary mb-2">
              Endpoint URL *
            </label>
            <input
              id="create-http-endpoint-url"
              type="url"
              value={state.httpEndpointUrl}
              onChange={(e) => onChange({ httpEndpointUrl: e.target.value })}
              placeholder="https://api.email-service.com/send"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-http-auth-header" className="block text-sm font-medium text-text-secondary mb-2">
              Auth Header
            </label>
            <input
              id="create-http-auth-header"
              type="password"
              value={state.httpAuthHeader}
              onChange={(e) => onChange({ httpAuthHeader: e.target.value })}
              placeholder="Bearer your-token"
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm"
            />
          </div>
          <div>
            <label htmlFor="create-http-payload-template" className="block text-sm font-medium text-text-secondary mb-2">
              Payload Template *
            </label>
            <textarea
              id="create-http-payload-template"
              value={state.httpPayloadTemplate}
              onChange={(e) => onChange({ httpPayloadTemplate: e.target.value })}
              placeholder={'{\n  "from": "{{ from_email }}",\n  "to": "{{ to_emails }}",\n  "subject": "{{ subject }}",\n  "html": "{{ html_body }}"\n}'}
              rows={6}
              disabled={submitting}
              className="w-full px-4 py-3 bg-surface border border-white/10 rounded-xl text-white placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-[#22C55E]/50 focus:border-[#22C55E]/50 transition-all text-sm font-mono resize-none"
            />
            <p className="text-xs text-text-tertiary mt-1">Jinja2 template with {'{{ from_email }}'}, {'{{ subject }}'}, {'{{ html_body }}'} variables</p>
          </div>
        </div>
      )}

      <div className="p-3 bg-[#22C55E]/10 rounded-lg border border-[#22C55E]/20 text-green-300 text-sm">
        <p className="font-medium">Email Connector</p>
        <p className="mt-2 text-xs opacity-80">
          Send email notifications and reports from MEHO investigations.
          Operations include: send_email and check_status.
        </p>
        <ol className="mt-2 ml-4 list-decimal text-xs space-y-1 opacity-80">
          <li>Configure your email provider and sender details</li>
          <li>MEHO will verify connectivity with a test email</li>
          <li>Use in investigations to email findings and reports</li>
        </ol>
      </div>
    </motion.div>
  );
}
