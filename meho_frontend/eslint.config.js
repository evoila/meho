import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import jsxA11y from 'eslint-plugin-jsx-a11y'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', '.vite', 'coverage']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.strict,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Allow underscore-prefixed variables (standard TS convention for intentionally unused params)
      'react-refresh/only-export-components': ['error', {
        allowConstantExport: true,
        allowExportNames: [
          'useKeycloakAuth', 'AuthProvider',       // auth provider barrel
          'CONNECTOR_COLORS',                       // ConnectorIcon constant map
          'buildConnectorStates',                   // OrchestratorProgress helper
          'toast',                                  // Toast sonner re-export
          // connector form validators — co-located with their components by design
          'validateRestForm', 'validateSoapForm', 'validateVmwareForm', 'validateProxmoxForm',
          'validateKubernetesForm', 'validateGcpForm', 'validateAzureForm', 'validateAwsForm',
          'validatePrometheusForm', 'validateLokiForm', 'validateTempoForm', 'validateAlertmanagerForm',
          'validateJiraForm', 'validateConfluenceForm', 'validateArgocdForm', 'validateGithubForm',
          'validateMcpForm', 'validateSlackForm', 'validateEmailForm',
        ],
      }],
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        destructuredArrayIgnorePattern: '^_',
      }],
    },
  },
  // Forbid export default in src/ (AGENTS.md absolute rule).
  // Scoped to src/ only — tool config files (vite, vitest, playwright) legitimately use default exports.
  {
    files: ['src/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-syntax': ['error', {
        selector: 'ExportDefaultDeclaration',
        message: 'export default is forbidden. Use named exports instead (AGENTS.md).',
      }],
    },
  },
  // jsx-a11y rules at recommended (error) severity.
  // Phase 64 fixed violations and promoted from warn.
  // Phase 67 resolved all label-has-associated-control warnings (htmlFor/id pairs).
  {
    files: ['**/*.{ts,tsx}'],
    ...jsxA11y.flatConfigs.recommended,
    rules: {
      ...jsxA11y.flatConfigs.recommended.rules,
      'jsx-a11y/label-has-associated-control': ['error', {
        assert: 'either',
        depth: 3,
      }],
    },
  },
])
