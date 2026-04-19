// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Main MEHO Application
 * 
 * Features:
 * - Keycloak authentication (production) or test-token (development)
 * - Code splitting for better performance
 * - Error boundary for error handling
 * - Toast notifications
 * - Skip links for accessibility
 */
import { lazy, Suspense } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './contexts/AuthContext';
import { TenantContextProvider } from './contexts/TenantContext';
import { ProtectedRoute } from './components/ProtectedRoute';
import { SuperadminRoute } from './components/SuperadminRoute';
import { Layout } from './components/Layout';
import { GlobalJobMonitor } from './components/jobs/GlobalJobMonitor';
import { ScreenReaderAnnouncer } from '@/shared/components/ScreenReaderAnnouncer';
import {
  ErrorBoundary,
  ToastProvider,
  SkipLink,
  LoadingState
} from '@/shared';
import { useRouteAnnouncer } from '@/shared/hooks/useRouteAnnouncer';
import { useLicense } from './hooks/useLicense';

// Lazy load pages for code splitting
const LoginPage = lazy(() => import('./pages/LoginPage').then(m => ({ default: m.LoginPage })));
const ChatPage = lazy(() => import('./pages/ChatPage').then(m => ({ default: m.ChatPage })));
const RecipesPage = lazy(() => import('./pages/RecipesPage').then(m => ({ default: m.RecipesPage })));
const KnowledgePage = lazy(() => import('./pages/KnowledgePage').then(m => ({ default: m.KnowledgePage })));
const ConnectorsPage = lazy(() => import('./pages/ConnectorsPage').then(m => ({ default: m.ConnectorsPage })));
const TopologyExplorerPage = lazy(() => import('./pages/TopologyExplorerPage').then(m => ({ default: m.TopologyExplorerPage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then(m => ({ default: m.SettingsPage })));

// Admin pages (global_admin only)
const AdminDashboardPage = lazy(() => import('./pages/admin/AdminDashboardPage').then(m => ({ default: m.AdminDashboardPage })));
const TenantsPage = lazy(() => import('./pages/admin/TenantsPage').then(m => ({ default: m.TenantsPage })));
const TenantSettingsPage = lazy(() => import('./pages/admin/TenantSettingsPage').then(m => ({ default: m.TenantSettingsPage })));

// Observability pages (TASK-186)
const SessionsPage = lazy(() => import('./pages/SessionsPage').then(m => ({ default: m.SessionsPage })));
const SessionTranscriptPage = lazy(() => import('./pages/SessionTranscriptPage').then(m => ({ default: m.SessionTranscriptPage })));

// Scheduled Tasks (Phase 45)
const ScheduledTasksPage = lazy(() => import('./pages/ScheduledTasksPage').then(m => ({ default: m.ScheduledTasksPage })));

// Orchestrator Skills (Phase 52)
const OrchestratorSkillsPage = lazy(() => import('./pages/OrchestratorSkillsPage').then(m => ({ default: m.OrchestratorSkillsPage })));

// Audit Log (Phase 58 - Security Hardening)
const AuditPage = lazy(() => import('./pages/AuditPage').then(m => ({ default: m.AuditPage })));

/**
 * Conditional redirect based on user role and edition.
 * Enterprise superadmins go to /admin, everyone else goes to /chat.
 */
function ConditionalRedirect() {
  const { user, isLoading } = useAuth();
  const license = useLicense();

  if (isLoading) {
    return <LoadingState message="Loading..." />;
  }

  // Redirect superadmins to admin dashboard (enterprise only)
  if (license.edition === 'enterprise' && user?.isGlobalAdmin) {
    return <Navigate to="/admin" replace />;
  }

  // Regular users (and community superadmins) go to chat
  return <Navigate to="/chat" replace />;
}

// Page loading fallback
function PageLoader() {
  return (
    <div className="h-full flex items-center justify-center">
      <LoadingState message="Loading page..." />
    </div>
  );
}

/** Focuses the page <h1> on route changes for screen reader context. */
function RouteAnnouncer() {
  useRouteAnnouncer();
  return null;
}

/**
 * Route guard: renders children in enterprise mode, redirects to /chat in community.
 * Used as element wrapper on enterprise-only routes (audit, admin).
 */
function EnterpriseRoute({ children }: Readonly<{ children: React.ReactNode }>) {
  const license = useLicense();
  if (license.edition !== 'enterprise') {
    return <Navigate to="/chat" replace />;
  }
  return <>{children}</>;
}

// Create a client
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 5 * 60 * 1000, // 5 minutes
    },
  },
});

function App() {
  return (
    <ErrorBoundary>
      <ToastProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <RouteAnnouncer />
            <AuthProvider>
              <SkipLink />
              {/* TenantContextProvider for superadmin tenant switching - TASK-140 Phase 2 */}
              <TenantContextProvider>
                  <Suspense fallback={<PageLoader />}>
                    <Routes>
                      {/* Public routes */}
                      <Route path="/login" element={<LoginPage />} />
                      <Route path="/login/callback" element={<Navigate to="/chat" replace />} />
                      
                      {/* Protected routes */}
                      <Route
                        path="/"
                        element={
                          <ProtectedRoute>
                            <Layout />
                          </ProtectedRoute>
                        }
                      >
                        <Route index element={<ConditionalRedirect />} />
                        <Route path="chat" element={<ChatPage />} />
                        <Route path="recipes" element={<RecipesPage />} />
                        <Route path="knowledge" element={<KnowledgePage />} />
                        <Route path="connectors" element={<ConnectorsPage />} />
                        <Route path="topology" element={<TopologyExplorerPage />} />
                        <Route path="settings" element={<SettingsPage />} />
                        
                        {/* Observability routes (TASK-186) */}
                        <Route path="sessions" element={<SessionsPage />} />
                        <Route path="sessions/:sessionId" element={<SessionTranscriptPage />} />

                        {/* Scheduled Tasks (Phase 45) */}
                        <Route path="scheduled-tasks" element={<ScheduledTasksPage />} />

                        {/* Orchestrator Skills (Phase 52) */}
                        <Route path="orchestrator-skills" element={<OrchestratorSkillsPage />} />

                        {/* Audit Log (Phase 58 - enterprise only, community redirects to /chat) */}
                        <Route path="audit" element={
                          <EnterpriseRoute>
                            <AuditPage />
                          </EnterpriseRoute>
                        } />

                        {/* Admin routes (enterprise + global_admin only) */}
                        <Route path="admin" element={
                          <EnterpriseRoute>
                            <SuperadminRoute>
                              <AdminDashboardPage />
                            </SuperadminRoute>
                          </EnterpriseRoute>
                        } />
                        <Route path="admin/tenants" element={
                          <EnterpriseRoute>
                            <SuperadminRoute>
                              <TenantsPage />
                            </SuperadminRoute>
                          </EnterpriseRoute>
                        } />
                        <Route path="admin/tenants/:tenantId" element={
                          <EnterpriseRoute>
                            <SuperadminRoute>
                              <TenantSettingsPage />
                            </SuperadminRoute>
                          </EnterpriseRoute>
                        } />
                      </Route>
                    </Routes>
                  </Suspense>
                  
                  {/* Global job monitor - visible from any page */}
                  <GlobalJobMonitor />
                  {/* Phase 64-02: Screen reader announcements - permanently in DOM */}
                  <ScreenReaderAnnouncer />
              </TenantContextProvider>
            </AuthProvider>
          </BrowserRouter>
        </QueryClientProvider>
      </ToastProvider>
    </ErrorBoundary>
  );
}

export default App;
