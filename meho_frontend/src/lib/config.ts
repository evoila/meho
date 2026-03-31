// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Frontend configuration
 * Reads from window.__RUNTIME_CONFIG__ injected by config.js at page load.
 * In Docker, config.js is generated at container startup via envsubst.
 * In local dev, public/config.js provides localhost defaults.
 */

interface RuntimeConfig {
  apiURL: string;
  keycloak: {
    url: string;
    realm: string;
    clientId: string;
  };
}

declare global {
  interface Window {
    __RUNTIME_CONFIG__?: RuntimeConfig;
  }
}

function getRuntimeConfig(): RuntimeConfig {
  if (typeof window !== 'undefined' && window.__RUNTIME_CONFIG__) {
    return window.__RUNTIME_CONFIG__;
  }
  return {
    apiURL: 'http://localhost:8000',
    keycloak: {
      url: 'http://localhost:8080',
      realm: 'meho-community',
      clientId: 'meho-frontend',
    },
  };
}

export const config = getRuntimeConfig();
