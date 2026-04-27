<!--
SPDX-License-Identifier: AGPL-3.0-only
Copyright (c) 2026 evoila Group
-->

# Connector Forms

This document describes the structure and conventions for the per-connector form
components that live under
`meho_frontend/src/components/connectors/forms/`.

## Overview

`CreateConnectorModal` is a routing shell. It owns shared state (`name`,
`description`, `connectorType`, `submitting`, `error`) and one typed state
object per connector type. It delegates all JSX and field-level validation to
a named form component.

```
meho_frontend/src/components/connectors/
  CreateConnectorModal.tsx       ← routing shell + handleSubmit
  forms/
    types.ts                     ← XxxFormState interfaces + DEFAULT_XXX_STATE exports
    RestForm.tsx
    SoapForm.tsx
    VmwareForm.tsx
    ProxmoxForm.tsx
    KubernetesForm.tsx
    GcpForm.tsx
    AzureForm.tsx
    AwsForm.tsx
    PrometheusForm.tsx
    LokiForm.tsx
    TempoForm.tsx
    AlertmanagerForm.tsx
    JiraForm.tsx
    ConfluenceForm.tsx
    ArgocdForm.tsx
    GithubForm.tsx
    McpForm.tsx
    SlackForm.tsx
    EmailForm.tsx
```

## Key types (`forms/types.ts`)

### `ConnectorFormBaseProps`

```typescript
export interface ConnectorFormBaseProps {
  submitting: boolean;
}
```

Every form component extends this interface with its own `state` and `onChange`.

### Per-type state interfaces and defaults

Each connector type has an `XxxFormState` interface and a `DEFAULT_XXX_STATE`
constant, both exported from `forms/types.ts`. The defaults are used to
initialize the corresponding `useState` hook in the modal.

Example:

```typescript
export interface KubernetesFormState {
  serverUrl: string;
  token: string;
  skipTls: boolean;
  routingDescription: string;
}

export const DEFAULT_KUBERNETES_STATE: KubernetesFormState = {
  serverUrl: '', token: '', skipTls: false, routingDescription: '',
};
```

### `HTTP_METHODS`

A string tuple of all HTTP verbs used in the REST safety-policy selector.
Also exported from `forms/types.ts` and imported statically by the modal.

## Form component anatomy

```typescript
// forms/KubernetesForm.tsx

export interface KubernetesFormProps extends ConnectorFormBaseProps {
  state: KubernetesFormState;
  onChange: (patch: Partial<KubernetesFormState>) => void;
}

export function validateKubernetesForm(state: KubernetesFormState): string | null {
  if (!state.serverUrl.trim()) return 'Kubernetes API server URL is required';
  if (!state.token.trim()) return 'Service Account token is required';
  return null;
}

export function KubernetesForm({ state, onChange, submitting }: KubernetesFormProps) {
  return (
    <motion.div ...>
      {/* form fields */}
    </motion.div>
  );
}
```

Rules:
- Named exports only (`export function`, never `export default`).
- `validateXxxForm` is a pure function — no side-effects, no hooks.
- The `{ state, onChange }` contract maps directly onto `{ state, dispatch }`
  from a `useReducer` (planned for task #281).

## Control flow in `CreateConnectorModal`

```tsx
function renderConnectorForm() {
  const base = { submitting };
  switch (connectorType) {
    case 'kubernetes':
      return (
        <KubernetesForm
          {...base}
          state={k8sState}
          onChange={(p) => setK8sState((prev) => ({ ...prev, ...p }))}
        />
      );
    // ... 18 more cases
  }
}
```

`isFormValid()` and the `handleSubmit` validation section use an identical
switch over `validateXxxForm` functions so validation and rendering always
agree.

## Special callbacks

### `RestFormProps.onApplyKubeconfig`

The kubeconfig import flow inside `RestForm` sets three fields that the modal
owns: `name`, `baseUrl`, and `authType`. The modal passes a callback:

```typescript
onApplyKubeconfig={({ name: n, baseUrl, authType }) => {
  setName(n);
  setRestState((prev) => ({ ...prev, baseUrl, authType }));
}}
```

### `GcpFormProps.onAutoFill`

GCP service-account JSON upload can auto-fill the connector name from the
JSON's `project_id` field. The modal passes:

```typescript
onAutoFill={({ name: n }) => { if (n) setName(n); }}
```

## Validation pattern

```typescript
// string | null — null means valid
export function validateXxxForm(state: XxxFormState): string | null {
  if (!state.requiredField.trim()) return 'Field is required';
  return null;
}
```

The modal calls `validateXxxForm` in three places:
1. `isFormValid()` — disables the submit button
2. `handleSubmit` validation block — shows an inline error before the API call
3. (Both prevent the server round-trip; the submit-button disable is a UX
   shortcut, not a security control.)

## Adding a new connector type

1. Add `XxxFormState` interface and `DEFAULT_XXX_STATE` to `forms/types.ts`.
2. Create `forms/XxxForm.tsx` exporting `XxxForm`, `XxxFormProps`, and
   `validateXxxForm`.
3. Add the `case 'xxx'` branch to `renderConnectorForm()`, `isFormValid()`,
   and the `handleSubmit` validation switch in `CreateConnectorModal.tsx`.
4. Add the API call for the new type inside `handleSubmit`.

## What task #281 changes

Task #281 (state consolidation) will replace the 19 individual `useState`
hooks in the modal with a single `useReducer`. The form components themselves
will not change — the `{ state, onChange }` interface is already shaped for it.
