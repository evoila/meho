// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Kubeconfig parser for importing Kubernetes cluster connection details.
 * 
 * Parses kubeconfig YAML and extracts connection information that can be used
 * to create a REST connector with OAUTH2 (Bearer token) authentication.
 */
import yaml from 'js-yaml';

// Kubeconfig structure types
interface KubeCluster {
  name: string;
  cluster: {
    server: string;
    'certificate-authority-data'?: string;
    'certificate-authority'?: string;
    'insecure-skip-tls-verify'?: boolean;
  };
}

interface KubeUser {
  name: string;
  user: {
    token?: string;
    username?: string;
    password?: string;
    'client-certificate-data'?: string;
    'client-certificate'?: string;
    'client-key-data'?: string;
    'client-key'?: string;
    exec?: {
      command: string;
      args?: string[];
      apiVersion?: string;
      env?: Array<{ name: string; value: string }>;
    };
  };
}

interface KubeContext {
  name: string;
  context: {
    cluster: string;
    user: string;
    namespace?: string;
  };
}

interface KubeConfig {
  apiVersion: string;
  kind: string;
  'current-context'?: string;
  clusters: KubeCluster[];
  contexts: KubeContext[];
  users: KubeUser[];
}

export type KubeAuthType = 'token' | 'basic' | 'client-cert' | 'exec' | 'unknown';

export interface KubeConnectionInfo {
  /** Context or cluster name */
  name: string;
  /** Kubernetes API server URL (e.g., https://cluster:6443) */
  server: string;
  /** Bearer token for authentication */
  token?: string;
  /** Username for basic auth */
  username?: string;
  /** Password for basic auth */
  password?: string;
  /** OpenAPI spec URL (always {server}/openapi/v3) */
  openapiUrl: string;
  /** Detected authentication type */
  authType: KubeAuthType;
  /** List of available contexts in the kubeconfig */
  availableContexts: string[];
  /** Warning message if auth type is not directly supported */
  authWarning?: string;
}

export interface KubeParseResult {
  success: boolean;
  info?: KubeConnectionInfo;
  error?: string;
}

/**
 * Parse a kubeconfig file and extract connection information.
 * 
 * @param content - The kubeconfig YAML content
 * @param context - Optional context name to use (defaults to current-context)
 * @returns Parsed connection information or error
 */
export function parseKubeconfig(content: string, context?: string): KubeParseResult { // NOSONAR (cognitive complexity)
  try {
    const config = yaml.load(content) as KubeConfig;
    
    if (!config || typeof config !== 'object') {
      return { success: false, error: 'Invalid kubeconfig: not a valid YAML object' };
    }
    
    // Validate basic structure
    if (!config.clusters || !Array.isArray(config.clusters) || config.clusters.length === 0) {
      return { success: false, error: 'Invalid kubeconfig: no clusters defined' };
    }
    
    if (!config.contexts || !Array.isArray(config.contexts) || config.contexts.length === 0) {
      return { success: false, error: 'Invalid kubeconfig: no contexts defined' };
    }
    
    if (!config.users || !Array.isArray(config.users)) {
      return { success: false, error: 'Invalid kubeconfig: no users defined' };
    }
    
    // Get available contexts
    const availableContexts = config.contexts.map(c => c.name);
    
    // Use specified context or current-context
    const contextName = context || config['current-context'];
    if (!contextName) {
      return { 
        success: false, 
        error: 'No context specified and no current-context set in kubeconfig' 
      };
    }
    
    // Find the context
    const ctx = config.contexts.find(c => c.name === contextName);
    if (!ctx) {
      return { 
        success: false, 
        error: `Context "${contextName}" not found in kubeconfig. Available: ${availableContexts.join(', ')}` 
      };
    }
    
    // Find the cluster
    const cluster = config.clusters.find(c => c.name === ctx.context.cluster);
    if (!cluster) {
      return { 
        success: false, 
        error: `Cluster "${ctx.context.cluster}" referenced by context "${contextName}" not found` 
      };
    }
    
    // Find the user
    const user = config.users.find(u => u.name === ctx.context.user);
    if (!user) {
      return { 
        success: false, 
        error: `User "${ctx.context.user}" referenced by context "${contextName}" not found` 
      };
    }
    
    // Extract server URL
    const server = cluster.cluster.server;
    if (!server) {
      return { success: false, error: 'Cluster does not have a server URL defined' };
    }
    
    // Determine auth type and extract credentials
    let authType: KubeAuthType = 'unknown';
    let token: string | undefined;
    let username: string | undefined;
    let password: string | undefined;
    let authWarning: string | undefined;
    
    if (user.user.token) {
      // Token-based auth (most common for service accounts)
      authType = 'token';
      token = user.user.token;
    } else if (user.user.username && user.user.password) {
      // Basic auth
      authType = 'basic';
      username = user.user.username;
      password = user.user.password;
    } else if (user.user['client-certificate-data'] || user.user['client-certificate']) {
      // Client certificate auth
      authType = 'client-cert';
      authWarning = 'Client certificate authentication detected. This is not directly supported. Please use a token-based kubeconfig or manually provide a bearer token.';
    } else if (user.user.exec) {
      // Exec-based auth (GKE, EKS, AKS)
      authType = 'exec';
      const execCmd = user.user.exec.command;
      const execArgs = user.user.exec.args?.join(' ') || '';
      authWarning = `Exec-based authentication detected (${execCmd}). Please run the following command locally and paste the token:\n\n${execCmd} ${execArgs}`;
    } else {
      authWarning = 'Could not determine authentication method. Please manually provide credentials.';
    }
    
    // Build OpenAPI URL
    const openapiUrl = `${server.replace(/\/$/, '')}/openapi/v3`;
    
    return {
      success: true,
      info: {
        name: contextName,
        server,
        token,
        username,
        password,
        openapiUrl,
        authType,
        availableContexts,
        authWarning,
      },
    };
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Unknown error parsing kubeconfig';
    return { success: false, error: `Failed to parse kubeconfig: ${message}` };
  }
}

/**
 * Get list of available contexts from a kubeconfig without full parsing.
 * Useful for showing a context selector dropdown.
 */
export function getKubeconfigContexts(content: string): string[] {
  try {
    const config = yaml.load(content) as KubeConfig;
    if (config?.contexts && Array.isArray(config.contexts)) {
      return config.contexts.map(c => c.name);
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Get the current-context from a kubeconfig.
 */
export function getCurrentContext(content: string): string | null {
  try {
    const config = yaml.load(content) as KubeConfig;
    return config?.['current-context'] || null;
  } catch {
    return null;
  }
}

