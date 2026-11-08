import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { LiveSnapshot } from "./types";

const mocks = vi.hoisted(() => ({
  getSession: vi.fn(),
  listProjects: vi.fn(),
  listExperiments: vi.fn(),
  getExperiment: vi.fn(),
  getLive: vi.fn(),
  listEvents: vi.fn(),
  clearLiveState: vi.fn(),
  logout: vi.fn(),
  approveDevice: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  resendVerification: vi.fn(),
  requestPasswordReset: vi.fn(),
  resetPassword: vi.fn(),
  claimProject: vi.fn(),
  verifyEmail: vi.fn(),
  createProject: vi.fn(),
  renameProject: vi.fn(),
  archiveProject: vi.fn(),
  listProjectCredentials: vi.fn(),
  createProjectCredential: vi.fn(),
  rotateProjectCredential: vi.fn(),
  revokeProjectCredential: vi.fn(),
}));

vi.mock("./api", () => ({
  api: mocks,
  streamUrl: (id: string, projectId: string) => `wss://backend.test/api/v1/experiments/${id}/stream?project_id=${projectId}`,
}));

vi.mock("./MetricPlot", () => ({
  default: ({ data }: { data: { x: number[]; y: (number | null)[] }[] }) => (
    <div data-testid="chart" data-epochs={data[0].x.join(",")} data-values={data[0].y.join(",")} />
  ),
}));

vi.mock("@xyflow/react", () => ({
  Background: () => null,
  Controls: () => null,
  MiniMap: () => null,
  ReactFlow: ({
    nodes,
    edges,
    onNodeClick,
  }: {
    nodes: { id: string; data: { kind: string; label: string; layerId: string } }[];
    edges: unknown[];
    onNodeClick: (_event: unknown, node: (typeof nodes)[number]) => void;
  }) => (
    <div data-testid="network" data-edges={edges.length}>
      {nodes.map((node) => (
        <button key={node.id} onClick={(event) => onNodeClick(event, node)}>
          {node.data.label}
        </button>
      ))}
    </div>
  ),
}));

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  open() { this.onopen?.(); }
  sendMessage(message: unknown) { this.onmessage?.({ data: JSON.stringify(message) }); }
  closeWithCode(code: number) { this.onclose?.({ code }); }
  close() {}
}

const snapshot: LiveSnapshot = {
  type: "snapshot",
  experiment_id: "exp-1",
  name: "Run alpha",
  framework: "pytorch",
  model_name: "MLP",
  status: "running",
  revision: 1,
  last_activity: "2026-01-01T10:00:00Z",
  architecture: {
    version: 1,
    framework: "pytorch",
    model_name: "MLP",
    classes: { "0": "cat" },
    layers: [{ layer_id: "hidden", type: "Linear", neuron_count: 2, neuron_ids: ["n0", "n1"] }],
  },
  current_epoch: 0,
  epochs: [0],
  live_neuron_buffers: {
    n0: [{ epoch: 0, metrics: { layer_id: "hidden", activation_mean: 0.25, gradient_mean: 0.1, weight_norm: 1.2 } }],
    n1: [{ epoch: 0, metrics: { layer_id: "hidden", inactive_fraction: 1 } }],
  },
  collapse_events: [],
};

describe("dashboard integration", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    vi.stubGlobal("confirm", vi.fn(() => true));
    mocks.getSession.mockResolvedValue({
      user: { id: "user-1", email: "owner@example.test" },
      csrf_token: "csrf",
    });
    mocks.listProjects.mockResolvedValue([
      { project_id: "project-1", name: "Demo project", description: null, created_at: "2026-01-01" },
    ]);
    mocks.listExperiments.mockResolvedValue([
      { experiment_id: "exp-1", name: "Run alpha", status: "running", latest_epoch: 0, framework: "pytorch" },
    ]);
    mocks.getExperiment.mockResolvedValue({
      experiment_id: "exp-1",
      project_id: "project-1",
      name: "Run alpha",
      framework: "pytorch",
      model_name: "MLP",
      status: "running",
      current_epoch: 0,
      architecture_registered: true,
      last_activity: snapshot.last_activity,
    });
    mocks.getLive.mockResolvedValue(structuredClone(snapshot));
    mocks.listEvents.mockResolvedValue({ experiment_id: "exp-1", events: [] });
    mocks.clearLiveState.mockResolvedValue({ status: "deleted", experiment_id: "exp-1" });
    mocks.logout.mockResolvedValue({ status: "logged_out" });
    mocks.listProjectCredentials.mockResolvedValue([]);
    mocks.createProject.mockImplementation(async (name: string) => ({
      project_id: "project-new",
      name,
      description: null,
      created_at: "2026-01-02T00:00:00Z",
    }));
    mocks.createProjectCredential.mockResolvedValue({
      credential_id: "credential-1",
      name: "Kaggle Training",
      created_at: "2026-01-02T00:00:00Z",
      revoked_at: null,
      is_legacy: false,
      project_id: "project-new",
      api_key: "ns_live_one_time",
    });
  });

  it("renders API architecture without invented edges and charts valid values", async () => {
    const { default: App } = await import("./App");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Run alpha/ }));

    expect(await screen.findByText("Network architecture")).toBeInTheDocument();
    expect(screen.getByTestId("network")).toHaveAttribute("data-edges", "0");
    expect(screen.getByTestId("network")).toHaveTextContent("hidden · 2");
    await screen.findByText("n0", { selector: "strong" });
    const chart = await screen.findByTestId("chart");
    expect(chart).toHaveAttribute("data-epochs", "0");
    expect(screen.getByTestId("chart")).toHaveAttribute("data-values", "0.25");
    expect(MockWebSocket.instances[0].url).toContain("wss://backend.test/");
    expect(MockWebSocket.instances[0].url).toContain("project_id=project-1");
    expect(MockWebSocket.instances[0].url).not.toContain("api_key");
  });

  it("applies websocket epoch updates to bounded metric charts", async () => {
    const { default: App } = await import("./App");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Run alpha/ }));
    await screen.findByTestId("network");
    await screen.findByText("n0", { selector: "strong" });

    act(() => MockWebSocket.instances[0].open());
    await waitFor(() => expect(mocks.getLive).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId("chart")).toHaveAttribute("data-epochs", "0"));
    act(() => {
      MockWebSocket.instances[0].sendMessage({
        type: "epoch_update",
        epoch: 1,
        revision: 2,
        neurons: {
          n0: { layer_id: "hidden", metrics: { layer_id: "hidden", activation_mean: 0.5 } },
        },
      });
    });
    await waitFor(() => expect(screen.getByTestId("chart")).toHaveAttribute("data-epochs", "0,1"));
    expect(screen.getByTestId("chart")).toHaveAttribute("data-values", "0.25,0.5");
  });

  it("reconnects after stream loss and invokes explicit live-state cleanup", async () => {
    const { default: App } = await import("./App");
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Run alpha/ }));
    await screen.findByText("n0", { selector: "strong" });
    expect(MockWebSocket.instances).toHaveLength(1);

    vi.useFakeTimers();
    act(() => MockWebSocket.instances[0].closeWithCode(1006));
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(MockWebSocket.instances).toHaveLength(2);
    vi.useRealTimers();

    fireEvent.click(screen.getByRole("button", { name: /Clear live state/ }));
    await waitFor(() => expect(mocks.clearLiveState).toHaveBeenCalledWith("exp-1", "project-1"));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("Persistent project and experiment metadata will remain"));
    expect(await screen.findByText(/Live in-memory state cleared/)).toBeInTheDocument();
  });

  it("requires login before discovering the user's projects and experiments", async () => {
    mocks.getSession.mockRejectedValueOnce(
      Object.assign(new Error("Dashboard login required"), { status: 401 }),
    );
    mocks.login.mockResolvedValue({
      user: { id: "user-1", email: "owner@example.test" },
      csrf_token: "csrf",
    });
    const { default: App } = await import("./App");
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "owner@example.test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "safe password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("button", { name: /Run alpha/ })).toBeInTheDocument();
    expect(mocks.login).toHaveBeenCalledWith("owner@example.test", "safe password");
    expect(mocks.listProjects).toHaveBeenCalled();
  });

  it("explains that a newly registered account can sign in when verification is disabled", async () => {
    mocks.getSession.mockRejectedValueOnce(
      Object.assign(new Error("Dashboard login required"), { status: 401 }),
    );
    mocks.register.mockResolvedValueOnce({ status: "registered" });
    const { default: App } = await import("./App");
    render(<App />);

    await screen.findByRole("heading", { name: "Sign in" });
    fireEvent.click(screen.getByRole("button", { name: "Create an account" }));
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "member@example.test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "a secure password 123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText("Account created. You can sign in now."))
      .toBeInTheDocument();
    expect(mocks.register).toHaveBeenCalledWith(
      "member@example.test",
      "a secure password 123",
    );
  });

  it("keeps the email-verification instructions when verification is required", async () => {
    mocks.getSession.mockRejectedValueOnce(
      Object.assign(new Error("Dashboard login required"), { status: 401 }),
    );
    mocks.register.mockResolvedValueOnce({ status: "verification_sent" });
    const { default: App } = await import("./App");
    render(<App />);

    await screen.findByRole("heading", { name: "Sign in" });
    fireEvent.click(screen.getByRole("button", { name: "Create an account" }));
    fireEvent.change(screen.getByLabelText("Email address"), {
      target: { value: "member@example.test" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "a secure password 123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    expect(await screen.findByText(
      "Check your email for a verification link before signing in.",
    )).toBeInTheDocument();
  });

  it("creates a project and displays a newly issued credential only in the issuance response", async () => {
    const { default: App } = await import("./App");
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Create a project"), {
      target: { value: "Kaggle workspace" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));

    expect(await screen.findByRole("option", { name: "Kaggle workspace" })).toBeInTheDocument();
    expect(mocks.createProject).toHaveBeenCalledWith("Kaggle workspace");
    fireEvent.click(
      screen.getByRole("button", { name: "Generate credential" }),
    );
    expect(await screen.findByText("Copy this credential now")).toBeInTheDocument();
    expect(screen.getByText("ns_live_one_time")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("ns_live_one_time")).not.toBeInTheDocument();
  });

  it("confirms project archive and removes it from the visible project list", async () => {
    mocks.archiveProject.mockResolvedValue({
      status: "archived",
      project_id: "project-1",
    });
    const { default: App } = await import("./App");
    render(<App />);
    fireEvent.click(await screen.findByText("Project settings & credentials"));
    fireEvent.click(await screen.findByRole("button", { name: "Archive project" }));

    await waitFor(() => expect(mocks.archiveProject).toHaveBeenCalledWith("project-1"));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("API credentials will be revoked"));
    expect(await screen.findByText("Project archived; persistent metadata was retained.")).toBeInTheDocument();
    expect(mocks.listExperiments).toHaveBeenCalledWith("project-1");
  });
});
