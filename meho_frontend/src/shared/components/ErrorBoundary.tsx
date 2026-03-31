// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle, RefreshCw, Home } from 'lucide-react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
  onError?: (error: Error, errorInfo: ErrorInfo) => void;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * Global Error Boundary Component
 * 
 * Catches JavaScript errors anywhere in the child component tree,
 * logs those errors, and displays a fallback UI.
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    this.props.onError?.(error, errorInfo);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  handleGoHome = () => {
    window.location.href = '/';
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="min-h-screen bg-background flex items-center justify-center p-6">
          <div className="max-w-md w-full glass rounded-2xl border border-white/10 p-8 text-center">
            <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-red-500/10 flex items-center justify-center border border-red-500/20">
              <AlertTriangle className="h-8 w-8 text-red-400" />
            </div>
            
            <h1 className="text-2xl font-bold text-white mb-2">
              Something went wrong
            </h1>
            
            <p className="text-text-secondary mb-6">
              An unexpected error occurred. Please try again or return to the home page.
            </p>

            {this.state.error && (
              <div className="mb-6 p-4 bg-red-500/5 border border-red-500/10 rounded-xl text-left">
                <p className="text-xs font-medium text-red-400 uppercase tracking-wider mb-1">
                  Error Details
                </p>
                <p className="text-sm text-red-300 font-mono break-all">
                  {this.state.error.message}
                </p>
              </div>
            )}

            <div className="flex gap-3">
              <button
                onClick={this.handleRetry}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-primary hover:bg-primary-hover text-white rounded-xl transition-all"
              >
                <RefreshCw className="h-4 w-4" />
                Try Again
              </button>
              <button
                onClick={this.handleGoHome}
                className="flex-1 flex items-center justify-center gap-2 px-4 py-2.5 bg-white/5 hover:bg-white/10 border border-white/10 text-white rounded-xl transition-all"
              >
                <Home className="h-4 w-4" />
                Go Home
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}

/**
 * Feature-level Error Boundary with a simpler UI
 * Use this for wrapping individual features/components
 */
export function FeatureErrorBoundary({ 
  children, 
  featureName = 'This feature',
  onRetry,
}: { 
  children: ReactNode; 
  featureName?: string;
  onRetry?: () => void;
}) {
  return (
    <ErrorBoundary
      fallback={
        <div className="p-6 bg-red-500/5 border border-red-500/10 rounded-xl text-center">
          <AlertTriangle className="h-8 w-8 text-red-400 mx-auto mb-3" />
          <h3 className="text-lg font-semibold text-white mb-2">
            {featureName} encountered an error
          </h3>
          <p className="text-sm text-text-secondary mb-4">
            Please refresh the page or try again later.
          </p>
          {onRetry && (
            <button
              onClick={onRetry}
              className="inline-flex items-center gap-2 px-4 py-2 bg-primary hover:bg-primary-hover text-white text-sm rounded-lg transition-all"
            >
              <RefreshCw className="h-4 w-4" />
              Try Again
            </button>
          )}
        </div>
      }
    >
      {children}
    </ErrorBoundary>
  );
}

