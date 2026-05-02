// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Superadmin Route Component
 * 
 * Protects routes that require global_admin role.
 * Redirects non-admin users to home page.
 */
import { Navigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { LoadingState } from '@/shared';

interface SuperadminRouteProps {
  children: React.ReactNode;
}

export function SuperadminRoute({ children }: Readonly<SuperadminRouteProps>) {
  const { user, isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="h-screen flex items-center justify-center bg-background">
        <LoadingState message="Checking permissions..." />
      </div>
    );
  }

  // Not authenticated - redirect to login
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  // Authenticated but not global admin - redirect to home
  if (!user?.isGlobalAdmin) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}

