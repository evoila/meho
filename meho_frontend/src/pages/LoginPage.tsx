// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Login Page
 * 
 * Supports email-first SSO authentication via Keycloak:
 * 1. User enters email to discover their organization
 * 2. Redirects to appropriate Keycloak realm for authentication
 * 
 * TASK-139 Phase 8: Email-first tenant discovery flow
 */
import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { AlertCircle, Info, ArrowRight, Loader2, Shield, LogIn, Mail, ArrowLeft, Building2 } from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import clsx from 'clsx';
import { config } from '../lib/config';
import { 
  storeDiscoveredTenant, 
  createKeycloakClient,
  type DiscoveredTenant 
} from '../lib/keycloak';

/**
 * Login steps for email-first flow
 */
type LoginStep = 'email' | 'sso';

export function LoginPage() {
  // Email-first flow state
  const [loginStep, setLoginStep] = useState<LoginStep>('email');
  const [email, setEmail] = useState('');
  const [discoveredTenant, setDiscoveredTenant] = useState<DiscoveredTenant | null>(null);
  const [isDiscovering, setIsDiscovering] = useState(false);
  const [discoveryError, setDiscoveryError] = useState('');
  
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  
  const { isAuthenticated, isLoading: isAuthLoading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  // Redirect if already authenticated
  useEffect(() => {
    if (!isAuthLoading && isAuthenticated) {
      navigate('/chat', { replace: true });
    }
  }, [isAuthLoading, isAuthenticated, navigate]);

  // Check for logout message from navigation state
  useEffect(() => {
    const state = location.state as { message?: string } | null;
    if (state?.message) {
      setInfo(state.message);
      // Clear the state after showing message
      navigate(location.pathname, { replace: true, state: {} });
    }
  }, [location, navigate]);

  // Email discovery handler
  const handleDiscoverTenant = async (e: React.FormEvent) => {
    e.preventDefault();
    setIsDiscovering(true);
    setDiscoveryError('');
    
    try {
      const response = await fetch(`${config.apiURL}/api/auth/discover-tenant`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim().toLowerCase() }),
      });
      
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || 'Organization not found');
      }
      
      const tenant: DiscoveredTenant = await response.json();
      setDiscoveredTenant(tenant);
      setLoginStep('sso');
      
      // Store for callback handling
      storeDiscoveredTenant(tenant);
    } catch (err) {
      setDiscoveryError(err instanceof Error ? err.message : 'Failed to discover organization');
    } finally {
      setIsDiscovering(false);
    }
  };

  // SSO login with discovered tenant
  const handleSSOLogin = () => {
    if (!discoveredTenant) return;
    
    setIsLoading(true);
    
    // Create Keycloak instance for the discovered realm
    const keycloakUrl = discoveredTenant.keycloak_url || config.keycloak.url || 'http://localhost:8080';
    const kc = createKeycloakClient(discoveredTenant.realm, keycloakUrl);
    
    // Initialize and login - keycloak-js handles PKCE automatically
    kc.init({ pkceMethod: 'S256' }).then(() => {
      kc.login({
        redirectUri: window.location.origin + '/login/callback',
      });
    }).catch((err) => {
      console.error('Failed to initialize Keycloak:', err);
      setError('Failed to initiate login. Please try again.');
      setIsLoading(false);
    });
  };

  // Direct Keycloak login for global admins (uses master realm)
  const handleDirectKeycloakLogin = () => {
    setIsLoading(true);
    
    const keycloakUrl = config.keycloak.url || 'http://localhost:8080';
    const realm = 'master';
    
    // Store the realm for callback handling
    storeDiscoveredTenant({
      tenant_id: realm,
      realm: realm,
      display_name: 'Global Admin',
      keycloak_url: keycloakUrl,
    });
    
    // Create Keycloak instance for master realm
    const kc = createKeycloakClient(realm, keycloakUrl);
    
    // Initialize and login - keycloak-js handles PKCE automatically
    kc.init({ pkceMethod: 'S256' }).then(() => {
      kc.login({
        redirectUri: window.location.origin + '/login/callback',
      });
    }).catch((err) => {
      console.error('Failed to initialize Keycloak:', err);
      setError('Failed to initiate login. Please try again.');
      setIsLoading(false);
    });
  };

  // Go back to email step
  const handleBackToEmail = () => {
    setLoginStep('email');
    setDiscoveredTenant(null);
    setDiscoveryError('');
  };

  // Show loading while checking auth state
  if (isAuthLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background relative overflow-hidden">
      {/* Background Effects */}
      <div className="absolute inset-0 overflow-hidden pointer-events-none">
        <div className="absolute top-[-10%] left-[-10%] w-[40%] h-[40%] rounded-full bg-primary/10 blur-[100px] animate-pulse-slow" />
        <div className="absolute bottom-[-10%] right-[-10%] w-[40%] h-[40%] rounded-full bg-secondary/10 blur-[100px] animate-pulse-slow" style={{ animationDelay: '1.5s' }} />
      </div>

      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.6, ease: "easeOut" }}
        className="w-full max-w-md mx-4 z-10"
      >
        <div className="glass rounded-2xl p-8 shadow-2xl border border-white/10 backdrop-blur-xl">
          {/* Header */}
          <div className="text-center mb-8">
            <motion.div
              initial={{ scale: 0.8, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              transition={{ delay: 0.2, duration: 0.5 }}
              className="w-16 h-16 mx-auto bg-gradient-to-br from-primary to-accent rounded-2xl flex items-center justify-center mb-4 shadow-lg shadow-primary/25"
            >
              <span className="text-3xl font-bold text-white">M</span>
            </motion.div>
            <h1 className="text-3xl font-bold text-white mb-2 tracking-tight">Welcome Back</h1>
            <p className="text-text-secondary">Sign in to access your AI assistant</p>
          </div>

          <AnimatePresence mode="wait">
            {info && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mb-6 p-3 bg-blue-500/10 border border-blue-500/20 rounded-xl flex items-start"
              >
                <Info className="h-5 w-5 text-blue-400 mr-2 flex-shrink-0 mt-0.5" />
                <span className="text-sm text-blue-200">{info}</span>
              </motion.div>
            )}

            {(error || discoveryError) && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: 'auto' }}
                exit={{ opacity: 0, height: 0 }}
                className="mb-6 p-3 bg-red-500/10 border border-red-500/20 rounded-xl flex items-start"
              >
                <AlertCircle className="h-5 w-5 text-red-400 mr-2 flex-shrink-0 mt-0.5" />
                <span className="text-sm text-red-200">{error || discoveryError}</span>
              </motion.div>
            )}
          </AnimatePresence>

          {/* Keycloak Login Flow */}
          <AnimatePresence mode="wait">
            {loginStep === 'email' && (
              <motion.div
                key="email-step"
                initial={{ opacity: 0, x: -20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 20 }}
                className="space-y-6"
              >
                <div className="text-center text-text-secondary text-sm mb-4">
                  <Shield className="inline-block w-4 h-4 mr-1" />
                  Secure authentication via SSO
                </div>
                
                <form onSubmit={handleDiscoverTenant} className="space-y-4">
                  <div>
                    <label htmlFor="email" className="block text-sm font-medium text-text-secondary mb-2">
                      Work Email
                    </label>
                    <div className="relative group">
                      <div className="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none">
                        <Mail className="h-5 w-5 text-text-tertiary group-focus-within:text-primary transition-colors" />
                      </div>
                      <input
                        id="email"
                        type="email"
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        placeholder="you@company.com"
                        className="w-full pl-10 pr-4 py-3 bg-surface/50 border border-border rounded-xl text-text-primary placeholder-text-tertiary focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
                        required
                        disabled={isDiscovering}
                      />
                    </div>
                    <p className="mt-2 text-xs text-text-tertiary">
                      Enter your work email to find your organization
                    </p>
                  </div>

                  <button
                    type="submit"
                    disabled={isDiscovering || !email.includes('@')}
                    className={clsx(
                      "w-full flex items-center justify-center px-4 py-3 rounded-xl font-medium text-white transition-all duration-200",
                      isDiscovering || !email.includes('@')
                        ? "bg-surface-active cursor-not-allowed opacity-50"
                        : "bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98]"
                    )}
                  >
                    {isDiscovering ? (
                      <>
                        <Loader2 className="animate-spin mr-2 h-5 w-5" />
                        Finding your organization...
                      </>
                    ) : (
                      <>
                        Continue
                        <ArrowRight className="ml-2 h-5 w-5" />
                      </>
                    )}
                  </button>
                </form>

                {/* Admin/Direct Access Link */}
                <div className="pt-4 border-t border-white/10">
                  <button
                    type="button"
                    onClick={handleDirectKeycloakLogin}
                    className="w-full text-center text-xs text-text-tertiary hover:text-text-secondary transition-colors"
                  >
                    Administrator? Sign in directly
                  </button>
                </div>
              </motion.div>
            )}

            {loginStep === 'sso' && discoveredTenant && (
              <motion.div
                key="sso-step"
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: -20 }}
                className="space-y-6"
              >
                {/* Back button */}
                <button
                  type="button"
                  onClick={handleBackToEmail}
                  className="flex items-center text-text-secondary hover:text-white transition-colors"
                >
                  <ArrowLeft className="h-4 w-4 mr-1" />
                  <span className="text-sm">Use a different email</span>
                </button>

                {/* Organization info */}
                <div className="p-4 bg-surface/50 border border-border rounded-xl">
                  <div className="flex items-center gap-3">
                    <div className="p-2 rounded-lg bg-primary/10">
                      <Building2 className="h-6 w-6 text-primary" />
                    </div>
                    <div>
                      <h3 className="text-white font-medium">
                        {discoveredTenant.display_name}
                      </h3>
                      <p className="text-sm text-text-secondary font-mono">
                        {discoveredTenant.tenant_id}
                      </p>
                    </div>
                  </div>
                </div>

                <p className="text-center text-sm text-text-secondary">
                  You will be redirected to your organization&apos;s login page
                </p>

                <button
                  type="button"
                  onClick={handleSSOLogin}
                  disabled={isLoading}
                  className={clsx(
                    "w-full flex items-center justify-center px-4 py-3 rounded-xl font-medium text-white transition-all duration-200",
                    isLoading
                      ? "bg-surface-active cursor-not-allowed opacity-50"
                      : "bg-gradient-to-r from-primary to-accent hover:shadow-lg hover:shadow-primary/25 hover:scale-[1.02] active:scale-[0.98]"
                  )}
                >
                  {isLoading ? (
                    <>
                      <Loader2 className="animate-spin mr-2 h-5 w-5" />
                      Redirecting to login...
                    </>
                  ) : (
                    <>
                      <LogIn className="mr-2 h-5 w-5" />
                      Sign in with SSO
                    </>
                  )}
                </button>
              </motion.div>
            )}
          </AnimatePresence>
        </div>

        <p className="mt-6 text-center text-xs text-text-tertiary">
          Secure Enterprise Access • MEHO Platform
        </p>
      </motion.div>
    </div>
  );
}
