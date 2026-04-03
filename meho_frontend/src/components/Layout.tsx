// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Main layout with navigation
 * 
 * Includes TenantContextBanner for superadmin tenant switching (TASK-140 Phase 2)
 */
import { Outlet, Link, useLocation, useNavigate } from 'react-router-dom';
import { MessageSquare, ChefHat, BookOpen, Plug, LogOut, Menu, X, Settings, Network, Building2, Activity, Clock, Brain, ClipboardList, PanelLeftClose, PanelLeftOpen } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { useTenantContext } from '../contexts/TenantContext';
import { TenantContextBanner } from './admin/TenantContextBanner';
import { useState, useRef, useCallback, useMemo, useEffect } from 'react';
import { useLicense } from '../hooks/useLicense';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import mehoLogo from '../assets/meho-logo-name.svg';
import mehoLogoIcon from '../assets/meho-logo.svg';
import { FirstRunTour } from './tour/FirstRunTour';

const SIDEBAR_COLLAPSED_KEY = 'meho-sidebar-collapsed';

export function Layout() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const { isInTenantContext } = useTenantContext();
  const license = useLicense();
  const [isMobileMenuOpen, setIsMobileMenuOpen] = useState(false);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true';
    } catch {
      return false;
    }
  });
  const hamburgerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(isSidebarCollapsed));
    } catch { /* localStorage unavailable */ }
  }, [isSidebarCollapsed]);

  const handleMobileMenuKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      setIsMobileMenuOpen(false);
      hamburgerRef.current?.focus();
    }
  }, []);

  const handleLogout = () => {
    logout();
    navigate('/login');
  };

  const navigation = useMemo(() => {
    const items = [
      { name: 'Chat', href: '/chat', icon: MessageSquare },
      { name: 'Recipes', href: '/recipes', icon: ChefHat },
      { name: 'Knowledge', href: '/knowledge', icon: BookOpen },
      { name: 'Connectors', href: '/connectors', icon: Plug },
      { name: 'Topology', href: '/topology', icon: Network },
      { name: 'Sessions', href: '/sessions', icon: Activity },
      { name: 'Scheduled Tasks', href: '/scheduled-tasks', icon: Clock },
      { name: 'Orchestrator Skills', href: '/orchestrator-skills', icon: Brain },
    ];

    if (license.edition === 'enterprise') {
      items.push({ name: 'Audit Log', href: '/audit', icon: ClipboardList });
    }

    items.push({ name: 'Settings', href: '/settings', icon: Settings });

    return items;
  }, [license.edition]);

  return (
    <div className={clsx(
      "h-screen flex bg-background text-text-primary overflow-hidden",
      // Add top padding when tenant context banner is visible
      isInTenantContext && "pt-12"
    )}>
      {/* Tenant Context Banner - TASK-140 Phase 2 (enterprise only, defense-in-depth) */}
      {license.edition === 'enterprise' && <TenantContextBanner />}
      
      {/* Sidebar - Desktop */}
      <aside
        className={clsx(
          'hidden md:flex flex-col glass border-r border-border z-20 transition-[width] duration-200',
          isSidebarCollapsed ? 'w-16' : 'w-64'
        )}
        aria-label="Main navigation"
      >
        <header className={clsx('flex items-center', isSidebarCollapsed ? 'justify-center p-3' : 'p-6')}>
          <img
            src={isSidebarCollapsed ? mehoLogoIcon : mehoLogo}
            alt="MEHO.ai - Home"
            className={isSidebarCollapsed ? 'h-8 w-8' : 'h-10 w-auto'}
          />
        </header>

        <nav className={clsx('flex-1 space-y-1 mt-4', isSidebarCollapsed ? 'px-2' : 'px-4 space-y-2')} aria-label="Primary">
          {navigation.map((item) => {
            const Icon = item.icon;
            const isActive = location.pathname === item.href;

            return (
              <Link
                key={item.name}
                to={item.href}
                title={isSidebarCollapsed ? item.name : undefined}
                className={clsx(
                  'relative flex items-center text-sm font-medium rounded-xl transition-all duration-200 group',
                  isSidebarCollapsed ? 'justify-center px-0 py-2.5' : 'px-4 py-3',
                  isActive
                    ? 'text-white bg-surface-active shadow-inner'
                    : 'text-text-secondary hover:text-white hover:bg-surface-hover'
                )}
              >
                {isActive && (
                  <motion.div
                    layoutId="activeTab"
                    className="absolute left-0 w-1 h-6 bg-primary rounded-r-full"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.2 }}
                  />
                )}
                <Icon className={clsx(
                  'h-5 w-5 transition-colors flex-shrink-0',
                  !isSidebarCollapsed && 'mr-3',
                  isActive ? 'text-primary' : 'text-text-tertiary group-hover:text-text-primary'
                )} />
                {!isSidebarCollapsed && item.name}
              </Link>
            );
          })}
          
          {/* Admin Section - Global Admin Only, Enterprise Only (hidden when viewing as tenant) */}
          {license.edition === 'enterprise' && user?.isGlobalAdmin && !isInTenantContext && (
            <>
              {!isSidebarCollapsed && (
                <div className="pt-4 mt-4 border-t border-border">
                  <span className="px-4 text-xs font-medium text-text-tertiary uppercase tracking-wider" >
                    Admin
                  </span>
                </div>
              )}
              {isSidebarCollapsed && <div className="my-2 border-t border-border" />}
              <Link
                to="/admin/tenants"
                title={isSidebarCollapsed ? 'Tenants' : undefined}
                className={clsx(
                  'relative flex items-center text-sm font-medium rounded-xl transition-all duration-200 group',
                  isSidebarCollapsed ? 'justify-center px-0 py-2.5' : 'px-4 py-3',
                  location.pathname.startsWith('/admin/tenants')
                    ? 'text-white bg-surface-active shadow-inner'
                    : 'text-text-secondary hover:text-white hover:bg-surface-hover'
                )}
              >
                {location.pathname.startsWith('/admin/tenants') && (
                  <motion.div
                    layoutId="activeTab"
                    className="absolute left-0 w-1 h-6 bg-primary rounded-r-full"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.2 }}
                  />
                )}
                <Building2 className={clsx(
                  'h-5 w-5 transition-colors flex-shrink-0',
                  !isSidebarCollapsed && 'mr-3',
                  location.pathname.startsWith('/admin/tenants') ? 'text-primary' : 'text-text-tertiary group-hover:text-text-primary'
                )} />
                {!isSidebarCollapsed && 'Tenants'}
              </Link>
            </>
          )}
        </nav>

        {/* Collapse toggle */}
        <div className={clsx('border-t border-border', isSidebarCollapsed ? 'px-2 py-2' : 'px-4 py-2')}>
          <button
            onClick={() => setIsSidebarCollapsed(prev => !prev)}
            className="w-full flex items-center justify-center p-2 rounded-lg text-text-tertiary hover:text-white hover:bg-surface-hover transition-colors"
            title={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={isSidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            {isSidebarCollapsed
              ? <PanelLeftOpen className="h-4 w-4" />
              : <PanelLeftClose className="h-4 w-4" />
            }
          </button>
        </div>

        <div className={clsx('border-t border-border', isSidebarCollapsed ? 'p-2' : 'p-4')} aria-label="User account">
          {isSidebarCollapsed ? (
            <div className="flex flex-col items-center gap-2">
              <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-gray-700 to-gray-600 flex items-center justify-center text-xs font-medium text-white ring-2 ring-border" title={user?.name || user?.email || 'User'}>
                {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
              </div>
              <button
                onClick={handleLogout}
                className="p-1.5 rounded-lg text-text-tertiary hover:text-white hover:bg-surface-hover transition-colors"
                title="Logout"
                aria-label="Log out"
              >
                <LogOut className="h-4 w-4" />
              </button>
            </div>
          ) : (
            <div className="flex items-center p-3 rounded-xl bg-surface/50 border border-border">
              <div className="w-8 h-8 rounded-full bg-gradient-to-tr from-gray-700 to-gray-600 flex items-center justify-center text-xs font-medium text-white ring-2 ring-border">
                {(user?.name || user?.email || 'U').charAt(0).toUpperCase()}
              </div>
              <div className="ml-3 flex-1 min-w-0">
                <p className="text-sm font-medium text-white truncate">{user?.name || user?.email || 'User'}</p>
                <p className="text-xs text-text-tertiary truncate">{user?.tenant_id || 'Tenant'}</p>
              </div>
              <button
                onClick={handleLogout}
                className="p-1.5 rounded-lg text-text-tertiary hover:text-white hover:bg-surface-hover transition-colors"
                title="Logout"
                aria-label="Log out"
              >
                <LogOut className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>
      </aside>

      {/* Mobile Header */}
      <header className="md:hidden fixed top-0 left-0 right-0 h-16 glass border-b border-border z-30 flex items-center justify-between px-4" aria-label="Mobile header">
        <img src={mehoLogo} alt="MEHO.ai - Home" className="h-8 w-auto" />
        <button
          ref={hamburgerRef}
          onClick={() => setIsMobileMenuOpen(!isMobileMenuOpen)}
          className="p-2 text-text-secondary hover:text-white"
          aria-expanded={isMobileMenuOpen}
          aria-controls="mobile-menu"
          aria-label={isMobileMenuOpen ? 'Close navigation menu' : 'Open navigation menu'}
        >
          {isMobileMenuOpen ? <X /> : <Menu />}
        </button>
      </header>

      {/* Mobile Menu Overlay */}
      <AnimatePresence>
        {isMobileMenuOpen && (
          <motion.div
            id="mobile-menu"
            initial={{ opacity: 0, y: -20 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -20 }}
            className="md:hidden fixed inset-0 top-16 bg-background z-20 p-4"
            onKeyDown={handleMobileMenuKeyDown}
          >
            <nav className="space-y-2" aria-label="Mobile navigation">
              {navigation.map((item) => {
                const Icon = item.icon;
                const isActive = location.pathname === item.href;
                return (
                  <Link
                    key={item.name}
                    to={item.href}
                    onClick={() => setIsMobileMenuOpen(false)}
                    className={clsx(
                      'flex items-center px-4 py-3 rounded-xl text-sm font-medium',
                      isActive ? 'bg-surface-active text-white' : 'text-text-secondary hover:bg-surface-hover'
                    )}
                  >
                    <Icon className="mr-3 h-5 w-5" />
                    {item.name}
                  </Link>
                );
              })}

              {/* Admin Section - Global Admin Only, Enterprise Only (Mobile, hidden when viewing as tenant) */}
              {license.edition === 'enterprise' && user?.isGlobalAdmin && !isInTenantContext && (
                <>
                  <div className="pt-4 mt-4 border-t border-border">
                    <span className="px-4 text-xs font-medium text-text-tertiary uppercase tracking-wider" >
                      Admin
                    </span>
                  </div>
                  <Link
                    to="/admin/tenants"
                    onClick={() => setIsMobileMenuOpen(false)}
                    className={clsx(
                      'flex items-center px-4 py-3 rounded-xl text-sm font-medium',
                      location.pathname.startsWith('/admin/tenants') ? 'bg-surface-active text-white' : 'text-text-secondary hover:bg-surface-hover'
                    )}
                  >
                    <Building2 className="mr-3 h-5 w-5" />
                    Tenants
                  </Link>
                </>
              )}

              <button
                onClick={handleLogout}
                className="w-full flex items-center px-4 py-3 rounded-xl text-sm font-medium text-red-400 hover:bg-surface-hover mt-4"
                aria-label="Log out"
              >
                <LogOut className="mr-3 h-5 w-5" />
                Logout
              </button>
            </nav>
          </motion.div>
        )}
      </AnimatePresence>

      {/* First-run guided tour (community edition only) */}
      <FirstRunTour />

      {/* Main Content */}
      <main id="main-content" className="flex-1 flex flex-col min-w-0 overflow-hidden relative" aria-label="Page content">
        {/* Background Ambient Glow */}
        <div className="absolute top-0 left-0 w-full h-full overflow-hidden pointer-events-none z-0">
          <div className="absolute top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-primary/5 blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-accent/5 blur-[120px]" />
        </div>

        <div className="flex-1 overflow-hidden relative z-10 pt-16 md:pt-0">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

