// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import { App } from './App.tsx'
import { bootstrapTransport } from '@/api/clients/transport'
import { config } from '@/lib/config'

// Lock the shared API transport to the runtime-provided `apiURL` *before*
// React mounts. Domain-client accessors (`getChatClient()`,
// `getConnectorsClient()`, ...) are called inside `useMemo`/render bodies,
// which means the first one to fire locks the transport's baseURL under
// first-caller-wins semantics. Without this bootstrap, whichever component
// happens to render first would pin the singleton to the hardcoded
// localhost default instead of the container's injected `config.apiURL`.
bootstrapTransport(config.apiURL);

const rootEl = document.getElementById('root');
if (!rootEl) throw new Error('Root element not found');
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
