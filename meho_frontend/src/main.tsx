// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const rootEl = document.getElementById('root');
if (!rootEl) throw new Error('Root element not found');
createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
