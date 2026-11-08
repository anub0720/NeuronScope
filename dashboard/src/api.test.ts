import { describe, expect, it, vi } from "vitest";
import { ApiError, createApiClient } from "./api";

describe("cookie-authenticated FastAPI client", () => {
  it("includes credentials on login and subsequent authenticated requests", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          user: { id: "u1", email: "u@example.test" },
          csrf_token: "csrf",
        }), { status: 200 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }));
    const client = createApiClient("https://neuronscope-api.onrender.com", fetcher);

    await client.login("u@example.test", "password");
    await client.listProjects();

    expect(fetcher.mock.calls[0][1]?.credentials).toBe("include");
    expect(fetcher.mock.calls[1][1]?.credentials).toBe("include");
  });

  it("uses the HttpOnly session cookie and scopes requests to a selected project", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ user: { id: "u1", email: "u@example.test" }, csrf_token: "csrf" }), { status: 200 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }));
    const client = createApiClient("https://backend.example.test/", fetcher);

    await client.getSession();
    await client.listExperiments("project-1");

    const [url, options] = fetcher.mock.calls[1];
    expect(url).toBe("https://backend.example.test/api/v1/experiments");
    expect(options?.credentials).toBe("include");
    expect(new Headers(options?.headers).get("X-Project-ID")).toBe("project-1");
    expect(new Headers(options?.headers).has("Authorization")).toBe(false);
  });

  it("requires CSRF protection for state-changing requests", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ user: { id: "u1", email: "u@example.test" }, csrf_token: "csrf" }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "deleted", experiment_id: "exp-1" }), { status: 200 }),
      );
    const client = createApiClient("", fetcher);
    await client.getSession();
    await client.clearLiveState("exp-1", "project-1");

    const [url, options] = fetcher.mock.calls[1];
    const headers = new Headers(options?.headers);
    expect(url).toBe("/api/v1/experiments/exp-1");
    expect(options?.method).toBe("DELETE");
    expect(options?.credentials).toBe("include");
    expect(headers.get("X-CSRF-Token")).toBe("csrf");
    expect(headers.get("X-Project-ID")).toBe("project-1");
  });

  it("surfaces API errors without treating them as successful snapshots", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ detail: "Dashboard login required" }), { status: 401 }),
    );
    const client = createApiClient("", fetcher);

    await expect(client.listProjects()).rejects.toMatchObject({
      message: "Dashboard login required",
      status: 401,
    } satisfies Partial<ApiError>);
  });

  it("creates and manages project credentials with cookie and CSRF authentication", async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          user: { id: "u1", email: "u@example.test" },
          csrf_token: "csrf",
        }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          project_id: "p1",
          name: "Kaggle project",
          description: null,
          created_at: "2026-01-01",
        }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({
          credential_id: "key1",
          name: "Kaggle Training",
          created_at: "2026-01-01",
          revoked_at: null,
          is_legacy: false,
          project_id: "p1",
          api_key: "ns_live_once",
        }), { status: 200 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200 }));
    const client = createApiClient("", fetcher);

    await client.getSession();
    await client.createProject("Kaggle project");
    const created = await client.createProjectCredential("p1", "Kaggle Training");
    const listed = await client.listProjectCredentials("p1");

    expect(created.api_key).toBe("ns_live_once");
    expect(listed).toEqual([]);
    const [, options] = fetcher.mock.calls[2];
    expect(new Headers(options?.headers).get("X-CSRF-Token")).toBe("csrf");
    expect(options?.credentials).toBe("include");
  });
});
