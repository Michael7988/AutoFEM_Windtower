/** Shared frontend API configuration and error type. */

const desktopApiBase = window.windTowerDesktop?.runtime.apiBase;

export const API_BASE = desktopApiBase ?? import.meta.env.VITE_API_BASE ?? "";
export const API_PROJECTS_ENDPOINT = `${API_BASE}/api/projects`;

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
    public readonly detail?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
