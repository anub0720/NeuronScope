import type {
  ExperimentDetail,
  ExperimentSummary,
  LiveSnapshot,
  IssuedProjectCredential,
  ProjectCredential,
  ProjectSummary,
} from "./types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/+$/, "");

export interface AuthUser {
  id: string;
  email: string;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function createApiClient(
  baseUrl: string,
  fetcher: typeof fetch = fetch,
) {
  let csrfToken = "";

  async function request<T>(
    path: string,
    init: RequestInit = {},
    projectId?: string,
  ): Promise<T> {
    let response: Response;
    try {
      const headers = new Headers(init.headers);
      if (init.body && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      if (projectId) headers.set("X-Project-ID", projectId);
      if (init.method && !["GET", "HEAD", "OPTIONS"].includes(init.method.toUpperCase())) {
        if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
      }
      response = await fetcher(
        `${baseUrl.replace(/\/+$/, "")}/api/v1${path}`,
        {
          ...init,
          credentials: "include",
          headers,
        },
      );
    } catch (error) {
      throw new ApiError(
        error instanceof Error ? error.message : "The backend could not be reached.",
      );
    }

    if (!response.ok) {
      let detail = `Request failed (${response.status})`;
      try {
        const body: unknown = await response.json();
        if (
          body &&
          typeof body === "object" &&
          "detail" in body &&
          typeof body.detail === "string"
        ) {
          detail = body.detail;
        }
      } catch {
        // HTTP errors may not include a JSON body.
      }
      throw new ApiError(detail, response.status);
    }
    if (response.status === 204) return undefined as T;
    return (await response.json()) as T;
  }

  async function withSession(path: string, init?: RequestInit) {
    const result = await request<{ user: AuthUser; csrf_token: string }>(path, init);
    csrfToken = result.csrf_token;
    return result;
  }

  return {
    getSession: () => withSession("/auth/session"),
    register: (email: string, password: string) =>
      request<{ status: string }>("/auth/register", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),
    resendVerification: (email: string, password: string) =>
      request<{ status: string }>("/auth/resend-verification", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),
    verifyEmail: (token: string) =>
      request<{ status: string }>("/auth/verify-email", {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    login: (email: string, password: string) =>
      withSession("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),
    requestPasswordReset: (email: string) =>
      request<{ status: string }>("/auth/password-reset/request", {
        method: "POST",
        body: JSON.stringify({ email }),
      }),
    resetPassword: (token: string, newPassword: string) =>
      request<{ status: string }>("/auth/password-reset/complete", {
        method: "POST",
        body: JSON.stringify({ token, new_password: newPassword }),
      }),
    logout: async () => {
      const result = await request<{ status: string }>("/auth/logout", {
        method: "POST",
      });
      csrfToken = "";
      return result;
    },
    approveDevice: (userCode: string) =>
      request<{ status: string }>("/auth/device/approve", {
        method: "POST",
        body: JSON.stringify({ user_code: userCode }),
      }),
    claimProject: (apiKey: string) =>
      request<{ project_id: string; name: string; status: string }>(
        "/auth/projects/claim",
        { method: "POST", body: JSON.stringify({ api_key: apiKey }) },
      ),
    listProjects: () => request<ProjectSummary[]>("/projects"),
    createProject: (name: string) =>
      request<ProjectSummary>("/projects", {
        method: "POST",
        body: JSON.stringify({ name }),
      }),
    renameProject: (projectId: string, name: string) =>
      request<ProjectSummary>(`/projects/${encodeURIComponent(projectId)}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      }),
    archiveProject: (projectId: string) =>
      request<{ status: string; project_id: string }>(
        `/projects/${encodeURIComponent(projectId)}`,
        { method: "DELETE" },
      ),
    listProjectCredentials: (projectId: string) =>
      request<ProjectCredential[]>(
        `/projects/${encodeURIComponent(projectId)}/credentials`,
      ),
    createProjectCredential: (projectId: string, name: string) =>
      request<IssuedProjectCredential>(
        `/projects/${encodeURIComponent(projectId)}/credentials`,
        { method: "POST", body: JSON.stringify({ name }) },
      ),
    rotateProjectCredential: (
      projectId: string,
      credentialId: string,
      name: string,
    ) =>
      request<IssuedProjectCredential>(
        `/projects/${encodeURIComponent(projectId)}/credentials/${encodeURIComponent(credentialId)}/rotate`,
        { method: "POST", body: JSON.stringify({ name }) },
      ),
    revokeProjectCredential: (projectId: string, credentialId: string) =>
      request<{ status: string; credential_id: string }>(
        `/projects/${encodeURIComponent(projectId)}/credentials/${encodeURIComponent(credentialId)}`,
        { method: "DELETE" },
      ),
    listExperiments: (projectId: string) =>
      request<ExperimentSummary[]>("/experiments", {}, projectId),
    getExperiment: (id: string, projectId: string) =>
      request<ExperimentDetail>(`/experiments/${encodeURIComponent(id)}`, {}, projectId),
    getLive: (id: string, projectId: string) =>
      request<LiveSnapshot>(`/experiments/${encodeURIComponent(id)}/live`, {}, projectId),
    listEvents: (id: string, projectId: string) =>
      request<{ experiment_id: string; events: LiveSnapshot["collapse_events"] }>(
        `/experiments/${encodeURIComponent(id)}/events`,
        {},
        projectId,
      ),
    clearLiveState: (id: string, projectId: string) =>
      request<{ status: string; experiment_id: string }>(
        `/experiments/${encodeURIComponent(id)}`,
        { method: "DELETE" },
        projectId,
      ),
  };
}

export const api = createApiClient(API_BASE);

export function streamUrl(experimentId: string, projectId: string): string {
  const base = API_BASE || window.location.origin;
  const url = new URL(
    `/api/v1/experiments/${encodeURIComponent(experimentId)}/stream`,
    base,
  );
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("project_id", projectId);
  return url.toString();
}
