// SPDX-License-Identifier: AGPL-3.0-only
// Copyright (c) 2026 evoila Group
/**
 * Scheduled tasks domain client (CRUD + runs + cron/schedule helpers).
 *
 * Mirrors the `/api/scheduled-tasks` backend surface: list/get/create/update/
 * delete, toggle, run-now, run history, and the NL-to-cron + cron-validation
 * helpers used by the Scheduled Task Modal.
 *
 * Migrated from `lib/api-client.ts` in Phase 4 (#350).
 */
import type { AxiosInstance } from 'axios';
import type {
  ScheduledTask,
  ScheduledTaskRun,
  CreateScheduledTaskRequest,
  UpdateScheduledTaskRequest,
  ParseScheduleResponse,
  ValidateCronResponse,
} from '../types';
import { getTransport } from './transport';

export function createScheduledTasksClient(transport: AxiosInstance) {
  return {
    async getScheduledTasks(): Promise<ScheduledTask[]> {
      const response = await transport.get<ScheduledTask[]>('/api/scheduled-tasks');
      return response.data;
    },

    async createScheduledTask(data: CreateScheduledTaskRequest): Promise<ScheduledTask> {
      const response = await transport.post<ScheduledTask>('/api/scheduled-tasks', data);
      return response.data;
    },

    async getScheduledTask(taskId: string): Promise<ScheduledTask> {
      const response = await transport.get<ScheduledTask>(`/api/scheduled-tasks/${taskId}`);
      return response.data;
    },

    async updateScheduledTask(
      taskId: string,
      data: UpdateScheduledTaskRequest,
    ): Promise<ScheduledTask> {
      const response = await transport.put<ScheduledTask>(
        `/api/scheduled-tasks/${taskId}`,
        data,
      );
      return response.data;
    },

    async deleteScheduledTask(taskId: string): Promise<void> {
      await transport.delete(`/api/scheduled-tasks/${taskId}`);
    },

    async toggleScheduledTask(taskId: string): Promise<ScheduledTask> {
      const response = await transport.patch<ScheduledTask>(
        `/api/scheduled-tasks/${taskId}/toggle`,
      );
      return response.data;
    },

    async runScheduledTaskNow(
      taskId: string,
    ): Promise<{ message: string; session_id: string }> {
      const response = await transport.post<{ message: string; session_id: string }>(
        `/api/scheduled-tasks/${taskId}/run`,
      );
      return response.data;
    },

    async getScheduledTaskRuns(
      taskId: string,
      limit?: number,
      offset?: number,
    ): Promise<ScheduledTaskRun[]> {
      const params = new URLSearchParams();
      if (limit !== undefined) params.set('limit', limit.toString());
      if (offset !== undefined) params.set('offset', offset.toString());
      const query = params.toString();
      const response = await transport.get<ScheduledTaskRun[]>(
        `/api/scheduled-tasks/${taskId}/runs${query ? `?${query}` : ''}`,
      );
      return response.data;
    },

    /**
     * Parse natural language schedule text ("every weekday at 8am") to a cron
     * expression using the backend LLM helper.
     */
    async parseSchedule(
      text: string,
      timezone: string,
    ): Promise<ParseScheduleResponse> {
      const response = await transport.post<ParseScheduleResponse>(
        '/api/scheduled-tasks/parse-schedule',
        { text, timezone },
      );
      return response.data;
    },

    /**
     * Generate a generic investigation prompt for scheduled tasks via LLM.
     *
     * Paired with the `generateEventPrompt` method on the connectors client
     * (reachable as `getConnectorsClient().generateEventPrompt(...)` — see
     * `meho_frontend/src/api/clients/connectors.ts`) which does the same job
     * for connector-event-scoped prompts. The two live on different clients
     * because the backend routes them under `/api/scheduled-tasks/...` vs
     * `/api/connectors/.../events/...` respectively.
     */
    async generateScheduledTaskPrompt(): Promise<{ prompt: string }> {
      const response = await transport.post<{ prompt: string }>(
        '/api/scheduled-tasks/generate-prompt',
        {},
      );
      return response.data;
    },

    async validateCron(
      cronExpression: string,
      timezone: string,
    ): Promise<ValidateCronResponse> {
      const response = await transport.post<ValidateCronResponse>(
        '/api/scheduled-tasks/validate-cron',
        { cron_expression: cronExpression, timezone },
      );
      return response.data;
    },

    async getTimezones(): Promise<string[]> {
      const response = await transport.get<string[]>('/api/scheduled-tasks/timezones');
      return response.data;
    },
  };
}

let scheduledTasksClient: ReturnType<typeof createScheduledTasksClient> | null = null;

export function getScheduledTasksClient(): ReturnType<typeof createScheduledTasksClient> {
  if (!scheduledTasksClient) {
    scheduledTasksClient = createScheduledTasksClient(getTransport());
  }
  return scheduledTasksClient;
}
