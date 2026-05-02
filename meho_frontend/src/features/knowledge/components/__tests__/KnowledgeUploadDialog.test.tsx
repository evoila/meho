// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Tests for KnowledgeUploadDialog versioning-mode behavior.
 *
 * We only assert the easily observable mode switch:
 * - In "new-version mode" (targetDocumentId present), the dialog shows the
 *   NEW VERSION badge with the provided family name and hides the URL tab.
 * - In regular upload mode, the URL tab is visible alongside the File tab.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { KnowledgeUploadDialog } from '../KnowledgeUploadDialog';
import { getKnowledgeClient } from '@/api/clients/knowledge';

vi.mock('@/api/clients/knowledge');

function wrap(node: ReactNode) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={queryClient}>{node}</QueryClientProvider>);
}

describe('KnowledgeUploadDialog -- version mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getKnowledgeClient).mockReturnValue({
      uploadDocument: vi.fn(),
      uploadDocumentVersion: vi.fn(),
      ingestUrl: vi.fn(),
      getJobStatus: vi.fn(),
    } as unknown as ReturnType<typeof getKnowledgeClient>);
  });

  it('renders NEW VERSION badge and hides the URL tab when targetDocumentId is provided', () => {
    wrap(
      <KnowledgeUploadDialog
        scope={{ scope_type: 'global' }}
        targetDocumentId="doc-42"
        targetFamilyName="VCF Admin Guide"
        inline
      />,
    );

    expect(screen.getByText('NEW VERSION')).toBeInTheDocument();
    expect(screen.getByText('VCF Admin Guide')).toBeInTheDocument();
    // URL tab is suppressed for version uploads.
    expect(screen.queryByRole('button', { name: /URL/ })).not.toBeInTheDocument();
  });

  it('renders both File and URL tabs in first-version (default) mode', () => {
    wrap(
      <KnowledgeUploadDialog
        scope={{ scope_type: 'global' }}
        inline
      />,
    );

    expect(screen.queryByText('NEW VERSION')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /File/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /URL/ })).toBeInTheDocument();
  });
});
