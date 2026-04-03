// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Global Job Monitor Component (Session 30 - Task 29)
 * 
 * Floating panel that shows all active upload jobs.
 * Visible from any page so users never lose track of uploads.
 * 
 * Features:
 * - Polls for active jobs every 1 second
 * - Shows mini progress bars
 * - Displays current status message
 * - Click to navigate to Knowledge page
 * - Toast notifications on completion
 */
import { useState, useEffect, useRef } from 'react';
import { Loader2, FileText, X, CheckCircle, AlertCircle } from 'lucide-react';
import { getAPIClient, type IngestionJobStatus } from '../../lib/api-client';
import { config } from '../../lib/config';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { useAuth } from '../../contexts/AuthContext';

export function GlobalJobMonitor() {
  const [activeJobs, setActiveJobs] = useState<IngestionJobStatus[]>([]);
  const [dismissedJobs, setDismissedJobs] = useState<Set<string>>(new Set());
  const previousJobIds = useRef<Set<string>>(new Set());
  const apiClient = getAPIClient(config.apiURL);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();

  useEffect(() => {
    // Don't poll if not authenticated
    if (!isAuthenticated) {
      return;
    }

    // Poll for active jobs every 1 second with in-flight guard
    let inFlight = false;
    const interval = setInterval(async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const jobs = await apiClient.getActiveJobs();

        // Detect jobs that just completed (Session 30: Auto-refresh document list)
        const currentJobIds = new Set(jobs.map(j => j.id));
        const completedJobs = Array.from(previousJobIds.current).filter(id => !currentJobIds.has(id));

        if (completedJobs.length > 0) {
          // Jobs completed! Refresh the document list
          console.log('Jobs completed, refreshing document list:', completedJobs);
          queryClient.invalidateQueries({ queryKey: ['knowledge-documents'] });
          queryClient.invalidateQueries({ queryKey: ['knowledge-chunks'] });
        }

        // Update previous job IDs for next comparison
        previousJobIds.current = currentJobIds;

        // Filter out dismissed jobs
        const visibleJobs = jobs.filter(job => !dismissedJobs.has(job.id));
        setActiveJobs(visibleJobs);

        // Clean up dismissed jobs that are no longer active
        const activeJobIds = new Set(jobs.map(j => j.id));
        setDismissedJobs(prev => {
          const next = new Set(prev);
          prev.forEach(id => {
            if (!activeJobIds.has(id)) {
              next.delete(id);
            }
          });
          return next;
        });
      } catch (err) {
        console.error('Failed to fetch active jobs:', err);
      } finally {
        inFlight = false;
      }
    }, 1000);

    // Initial fetch
    apiClient.getActiveJobs().then(jobs => {
      const visibleJobs = jobs.filter(job => !dismissedJobs.has(job.id));
      setActiveJobs(visibleJobs);
      previousJobIds.current = new Set(jobs.map(j => j.id));
    }).catch(err => {
      console.error('Failed to fetch active jobs:', err);
    });

    return () => clearInterval(interval);
  }, [apiClient, dismissedJobs, queryClient, isAuthenticated]);

  const handleDismiss = (jobId: string) => {
    setDismissedJobs(prev => new Set(prev).add(jobId));
    setActiveJobs(prev => prev.filter(job => job.id !== jobId));
  };

  const handleView = () => {
    navigate('/knowledge');
  };

  if (activeJobs.length === 0) {
    return null;
  }

  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2 max-w-sm">
      {activeJobs.map((job) => (
        <ActiveJobCard
          key={job.id}
          job={job}
          onDismiss={handleDismiss}
          onView={handleView}
        />
      ))}
    </div>
  );
}

interface ActiveJobCardProps {
  job: IngestionJobStatus;
  onDismiss: (jobId: string) => void;
  onView: () => void;
}

function ActiveJobCard({ job, onDismiss, onView }: Readonly<ActiveJobCardProps>) {
  // Determine filename from job (if available in future enhancement)
  const filename = `document-${job.id.slice(0, 8)}`;
  
  // Calculate progress percentage
  const progressPercent = job.progress?.overall_progress !== undefined
    ? Math.round(job.progress.overall_progress * 100)
    : job.progress?.percent || 0;

  // Get status message
  const statusMessage = job.progress?.status_message || 'Processing...';

  return (
    <div className="bg-white shadow-lg rounded-lg p-3 border border-gray-200 animate-slide-in-right">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2 flex-1 min-w-0">
          {job.status === 'processing' && <Loader2 className="h-4 w-4 text-blue-600 animate-spin flex-shrink-0" />}
          {job.status === 'completed' && <CheckCircle className="h-4 w-4 text-green-600 flex-shrink-0" />}
          {job.status === 'failed' && <AlertCircle className="h-4 w-4 text-red-600 flex-shrink-0" />}
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <FileText className="h-4 w-4 text-gray-400 flex-shrink-0" />
              <span className="text-sm font-medium truncate" title={filename}>
                {filename}
              </span>
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 ml-2">
          <button
            onClick={onView}
            className="text-xs text-blue-600 hover:underline px-2 py-1"
            title="View in Knowledge page"
          >
            View
          </button>
          <button
            onClick={() => onDismiss(job.id)}
            className="text-gray-400 hover:text-gray-600 p-1 rounded hover:bg-gray-100"
            title="Dismiss"
          >
            <X className="h-3 w-3" />
          </button>
        </div>
      </div>

      {/* Mini progress bar */}
      <div className="mb-1">
        <div className="w-full bg-gray-200 rounded-full h-1.5">
          <div
            className={`h-1.5 rounded-full transition-all ${
              ({ failed: 'bg-red-500', completed: 'bg-green-500' } as Record<string, string>)[job.status] ?? 'bg-blue-600'
            }`}
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </div>

      {/* Status text */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-gray-600 truncate" title={statusMessage}>
          {statusMessage}
        </p>
        <span className="text-xs text-gray-500 ml-2">{progressPercent}%</span>
      </div>

      {/* Error message if failed */}
      {job.status === 'failed' && job.error && (
        <p className="text-xs text-red-600 mt-1 truncate" title={job.error}>
          {job.error}
        </p>
      )}
    </div>
  );
}

// Add animation for sliding in from right
const style = document.createElement('style');
style.textContent = `
  @keyframes slide-in-right {
    from {
      transform: translateX(100%);
      opacity: 0;
    }
    to {
      transform: translateX(0);
      opacity: 1;
    }
  }
  .animate-slide-in-right {
    animation: slide-in-right 0.3s ease-out;
  }
`;
document.head.appendChild(style);

