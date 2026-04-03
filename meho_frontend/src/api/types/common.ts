// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Common API Types
 * 
 * Shared types used across the API.
 */

export interface APIError {
  message: string;
  type: string;
  status_code: number;
}

