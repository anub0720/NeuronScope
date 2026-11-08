import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { api, streamUrl, type AuthUser } from "./api";
import {
  applySocketMessage,
  metricValue,
  usableMetric,
  visibleNeuronIds,
} from "./dashboardState";
import type {
  CollapseEvent,
  ExperimentDetail,
  ExperimentSummary,
  IssuedProjectCredential,
  LiveSnapshot,
  NeuronMetrics,
  ProjectCredential,
  ProjectSummary,
  SocketStatus,
} from "./types";

const METRICS = [
  { key: "activation_mean", label: "Activation mean", color: "#7c9cff" },
  { key: "activation_variance", label: "Activation variance", color: "#b69cff" },
  { key: "gradient_mean", label: "Gradient mean", color: "#52d6b7" },
  { key: "gradient_variance", label: "Gradient variance", color: "#f4b35e" },
  { key: "weight_norm", label: "Weight norm", color: "#f18da7" },
] as const;
const MAX_NEURONS_PER_LAYER = 24;
const MetricPlot = lazy(() => import("./MetricPlot"));

interface FlowData extends Record<string, unknown> {
  kind: "layer" | "neuron";
  label: string;
  layerId: string;
  state?: "active" | "inactive" | "anomaly" | "unavailable";
  hint?: string;
}

type FlowNode = Node<FlowData>;

function FlowCard({ data }: NodeProps<FlowNode>) {
  if (data.kind === "layer") {
    return (
      <div className="layer-label">
        <span className="layer-icon">▤</span>
        <span>{data.label}</span>
      </div>
    );
  }
  return (
    <div className={`neuron-node ${data.state ?? "unavailable"}`}>
      <span className="neuron-dot" />
      <span className="neuron-id" title={data.label}>{data.label}</span>
      <span className="neuron-hint">{data.hint}</span>
    </div>
  );
}

const nodeTypes = { card: FlowCard };

function formatValue(value: unknown, digits = 4): string {
  if (!usableMetric(value)) return "—";
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: digits,
  }).format(value);
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "—"
    : date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof Error && "status" in error && error.status === 401;
}

function statusClass(status: string): string {
  if (status === "running" || status === "active") return "running";
  if (status === "finished" || status === "recovered") return "finished";
  return "neutral";
}

export default function App() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMode, setAuthMode] = useState<"login" | "register" | "reset">("login");
  const [authEmail, setAuthEmail] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authNotice, setAuthNotice] = useState("");
  const [canResendVerification, setCanResendVerification] = useState(false);
  const [resetToken, setResetToken] = useState("");
  const [deviceCode, setDeviceCode] = useState("");
  const [legacyProjectKey, setLegacyProjectKey] = useState("");
  const [claimError, setClaimError] = useState("");
  const [claimBusy, setClaimBusy] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [projectBusy, setProjectBusy] = useState(false);
  const [projectError, setProjectError] = useState("");
  const [renameProjectName, setRenameProjectName] = useState("");
  const [credentials, setCredentials] = useState<ProjectCredential[]>([]);
  const [credentialsLoading, setCredentialsLoading] = useState(false);
  const [credentialName, setCredentialName] = useState("Kaggle Training");
  const [credentialBusy, setCredentialBusy] = useState(false);
  const [credentialError, setCredentialError] = useState("");
  const [issuedCredential, setIssuedCredential] =
    useState<IssuedProjectCredential | null>(null);
  const [copyError, setCopyError] = useState("");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectsError, setProjectsError] = useState("");
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [experiments, setExperiments] = useState<ExperimentSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [snapshot, setSnapshot] = useState<LiveSnapshot | null>(null);
  const [detailError, setDetailError] = useState("");
  const [socketStatus, setSocketStatus] = useState<SocketStatus>("disconnected");
  const [socketError, setSocketError] = useState("");
  const [lastMessageAt, setLastMessageAt] = useState<number | null>(null);
  const [selectedLayerId, setSelectedLayerId] = useState("");
  const [selectedNeuronId, setSelectedNeuronId] = useState("");
  const [metricKey, setMetricKey] =
    useState<(typeof METRICS)[number]["key"]>("activation_mean");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [suppressStream, setSuppressStream] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const loadSequence = useRef(0);
  const projectDiscovery = useRef({ inFlight: false, failures: 0, nextAt: 0 });
  const experimentDiscovery = useRef({ inFlight: false, failures: 0, nextAt: 0 });

  const refreshProjects = useCallback(async () => {
    const discovery = projectDiscovery.current;
    if (discovery.inFlight || Date.now() < discovery.nextAt) return;
    discovery.inFlight = true;
    try {
      const results = await api.listProjects();
      setProjects(results);
      setProjectsError("");
      discovery.failures = 0;
      discovery.nextAt = 0;
      setSelectedProjectId((current) =>
        results.some((project) => project.project_id === current)
          ? current
          : results[0]?.project_id ?? "",
      );
    } catch (error) {
      setProjectsError(errorText(error));
      discovery.failures += 1;
      discovery.nextAt =
        Date.now() + Math.min(60_000, 5_000 * 2 ** (discovery.failures - 1));
      if (isUnauthorized(error)) {
        setAuthError("Your dashboard session expired. Sign in again.");
        setUser(null);
      }
    } finally {
      discovery.inFlight = false;
    }
  }, []);

  const refreshExperiments = useCallback(async () => {
    if (!selectedProjectId) {
      setExperiments([]);
      setSelectedId("");
      setListLoading(false);
      return;
    }
    const discovery = experimentDiscovery.current;
    if (discovery.inFlight || Date.now() < discovery.nextAt) return;
    discovery.inFlight = true;
    setListError("");
    try {
      const next = await api.listExperiments(selectedProjectId);
      setExperiments(next);
      discovery.failures = 0;
      discovery.nextAt = 0;
      setSelectedId((current) =>
        next.some((experiment) => experiment.experiment_id === current)
          ? current
          : "",
      );
    } catch (error) {
      setListError(errorText(error));
      discovery.failures += 1;
      discovery.nextAt =
        Date.now() + Math.min(60_000, 5_000 * 2 ** (discovery.failures - 1));
      if (isUnauthorized(error)) {
        setAuthError("Your dashboard session expired. Sign in again.");
        setUser(null);
      }
    } finally {
      setListLoading(false);
      discovery.inFlight = false;
    }
  }, [selectedProjectId]);

  const refreshProjectCredentials = useCallback(async (projectId: string) => {
    setCredentialsLoading(true);
    setCredentialError("");
    try {
      const result = await api.listProjectCredentials(projectId);
      setCredentials(result);
    } catch (error) {
      setCredentialError(errorText(error));
    } finally {
      setCredentialsLoading(false);
    }
  }, []);

  const refreshSelected = useCallback(async (id: string, projectId: string) => {
    const sequence = ++loadSequence.current;
    setDetailError("");
    try {
      const currentDetail = await api.getExperiment(id, projectId);
      if (sequence !== loadSequence.current) return;
      setDetail(currentDetail);
    } catch (error) {
      if (sequence === loadSequence.current) setDetailError(errorText(error));
      return;
    }

    const [liveResult, eventResult] = await Promise.allSettled([
      api.getLive(id, projectId),
      api.listEvents(id, projectId),
    ]);
    if (sequence !== loadSequence.current) return;
    if (liveResult.status === "fulfilled") {
      const next = liveResult.value;
      if (eventResult.status === "fulfilled") {
        next.collapse_events = eventResult.value.events;
      }
      setSnapshot((current) =>
        current && current.revision > next.revision ? current : next,
      );
      setDetailError("");
      setLastMessageAt(Date.now());
    } else {
      setSnapshot(null);
      setDetailError(
        `Experiment metadata is available, but live state is unavailable: ${errorText(liveResult.reason)}`,
      );
    }
  }, []);

  useEffect(() => {
    let active = true;
    const params = new URLSearchParams(window.location.search);
    const verificationToken = params.get("verify_token");
    const pendingResetToken = params.get("reset_token");
    const pendingDeviceCode = params.get("device_code");
    if (pendingResetToken) {
      setResetToken(pendingResetToken);
      setAuthMode("reset");
    }
    if (pendingDeviceCode) setDeviceCode(pendingDeviceCode);
    if (verificationToken) {
      void api.verifyEmail(verificationToken)
        .then(() => setAuthNotice("Email verified. Sign in to continue."))
        .catch((error: unknown) => setAuthError(errorText(error)));
      params.delete("verify_token");
      window.history.replaceState({}, "", `${window.location.pathname}${params.size ? `?${params}` : ""}`);
    }
    if (pendingResetToken) {
      setAuthLoading(false);
      return () => { active = false; };
    }
    void api.getSession()
      .then(({ user: currentUser }) => {
        if (active) setUser(currentUser);
      })
      .catch((error: unknown) => {
        if (!isUnauthorized(error) && active) setAuthError(errorText(error));
      })
      .finally(() => {
        if (active) setAuthLoading(false);
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!user) {
      setProjects([]);
      setExperiments([]);
      setSelectedProjectId("");
      setSelectedId("");
      return;
    }
    void refreshProjects();
    const timer = window.setInterval(() => void refreshProjects(), 15_000);
    return () => window.clearInterval(timer);
  }, [user, refreshProjects]);

  useEffect(() => {
    if (!user || !selectedProjectId) {
      setExperiments([]);
      setSelectedId("");
      setListLoading(false);
      return;
    }
    setListLoading(true);
    void refreshExperiments();
    const timer = window.setInterval(() => void refreshExperiments(), 5_000);
    return () => window.clearInterval(timer);
  }, [user, selectedProjectId, refreshExperiments]);

  useEffect(() => {
    const project = projects.find((item) => item.project_id === selectedProjectId);
    setRenameProjectName(project?.name ?? "");
    setCredentials([]);
    setIssuedCredential(null);
    setCopyError("");
    if (selectedProjectId) void refreshProjectCredentials(selectedProjectId);
  }, [selectedProjectId, refreshProjectCredentials]);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setSnapshot(null);
      setDetailError("");
      return;
    }
    setDetail(null);
    setSnapshot(null);
    setSelectedLayerId("");
    setSelectedNeuronId("");
    setSuppressStream(false);
    if (selectedProjectId) void refreshSelected(selectedId, selectedProjectId);
  }, [selectedId, selectedProjectId, refreshSelected]);

  useEffect(() => {
    if (!selectedId || !selectedProjectId || !user || suppressStream) {
      setSocketStatus("disconnected");
      return;
    }
    let disposed = false;
    let retryTimer: number | undefined;
    let retryDelay = 1000;
    let terminalFailure = false;
    let intentionallyClosed = false;

    const connect = () => {
      if (disposed || terminalFailure) return;
      setSocketStatus(retryDelay > 1000 ? "reconnecting" : "connecting");
      let socket: WebSocket;
      try {
        socket = new WebSocket(streamUrl(selectedId, selectedProjectId));
      } catch (error) {
        setSocketError(errorText(error));
        scheduleRetry();
        return;
      }
      socketRef.current = socket;
      socket.onopen = () => {
        retryDelay = 1000;
        setSocketStatus("connected");
        setSocketError("");
        setLastMessageAt(Date.now());
        void refreshSelected(selectedId, selectedProjectId);
      };
      socket.onmessage = (frame) => {
        try {
          const message = JSON.parse(frame.data) as Record<string, unknown>;
          if (message.type === "experiment_deleted" || message.type === "experiment_expired") {
            intentionallyClosed = true;
            setSnapshot(null);
            setSocketStatus("disconnected");
            setSocketError("Live experiment state was removed or expired.");
            void refreshExperiments();
            return;
          }
          setSnapshot((current) =>
            current ? applySocketMessage(current, message) : current,
          );
          setLastMessageAt(Date.now());
        } catch {
          setSocketError("Received an unreadable update from the backend.");
        }
      };
      socket.onerror = () => {
        setSocketError("Live stream interrupted; attempting to reconnect.");
      };
      socket.onclose = (event) => {
        if (disposed || intentionallyClosed) return;
        if (event.code === 4401) {
          terminalFailure = true;
          setSocketStatus("disconnected");
          setSocketError("Your dashboard session has expired. Sign in again.");
          setAuthError("Your dashboard session expired. Sign in again.");
          setUser(null);
          return;
        }
        if (event.code === 4403) {
          terminalFailure = true;
          setSocketStatus("disconnected");
          setSocketError("WebSocket origin is not allowed. Check backend DASHBOARD_ORIGINS.");
          return;
        }
        setSocketStatus("reconnecting");
        if (event.code === 4404) {
          setSocketError("Live state is unavailable. Reconnecting while metadata remains.");
        }
        scheduleRetry();
      };
    };

    const scheduleRetry = () => {
      if (disposed || terminalFailure) return;
      setSocketStatus("reconnecting");
      retryTimer = window.setTimeout(connect, retryDelay);
      retryDelay = Math.min(retryDelay * 2, 30000);
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [selectedId, selectedProjectId, user, refreshExperiments, refreshSelected, suppressStream]);

  useEffect(() => {
    if (!selectedId || !selectedProjectId || !user) return;
    const timer = window.setInterval(
      () => void refreshSelected(selectedId, selectedProjectId),
      15_000,
    );
    return () => window.clearInterval(timer);
  }, [selectedId, selectedProjectId, user, refreshSelected]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 5000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const architecture = snapshot?.architecture ?? null;
  const layers = architecture?.layers ?? [];
  const selectedLayer =
    layers.find((layer) => layer.layer_id === selectedLayerId) ?? layers[0];
  const layerNeuronIds = selectedLayer
    ? visibleNeuronIds(
        selectedLayer.layer_id,
        selectedLayer.neuron_count,
        selectedLayer.neuron_ids,
        snapshot?.live_neuron_buffers ?? {},
      )
    : [];
  const activeEvents = snapshot?.collapse_events.filter((event) => event.status === "active") ?? [];

  useEffect(() => {
    if (!selectedLayer) {
      setSelectedLayerId("");
      setSelectedNeuronId("");
      return;
    }
    if (selectedLayerId !== selectedLayer.layer_id) {
      setSelectedLayerId(selectedLayer.layer_id);
      return;
    }
    if (!layerNeuronIds.includes(selectedNeuronId)) {
      setSelectedNeuronId(layerNeuronIds[0] ?? "");
    }
  }, [selectedLayer, selectedLayerId, layerNeuronIds, selectedNeuronId]);

  const selectedRows = snapshot?.live_neuron_buffers[selectedNeuronId] ?? [];
  const latestRow = selectedRows.at(-1);
  const selectedMetric = METRICS.find((item) => item.key === metricKey) ?? METRICS[0];
  const chartData = useMemo(
    () => [
      {
        type: "scatter" as const,
        mode: "lines+markers" as const,
        name: selectedMetric.label,
        x: selectedRows.map((row) => row.epoch),
        y: selectedRows.map((row) => metricValue(row, metricKey)),
        connectgaps: false,
        line: { color: selectedMetric.color, width: 2 },
        marker: { color: selectedMetric.color, size: 7 },
        hovertemplate: "Epoch %{x}<br>%{y}<extra></extra>",
      },
    ],
    [metricKey, selectedMetric.color, selectedMetric.label, selectedRows],
  );

  const flowNodes = useMemo<FlowNode[]>(() => {
    if (!architecture) return [];
    const result: FlowNode[] = [];
    architecture.layers.forEach((layer, layerIndex) => {
      const ids = visibleNeuronIds(
        layer.layer_id,
        layer.neuron_count,
        layer.neuron_ids,
        snapshot?.live_neuron_buffers ?? {},
      );
      const displayed = ids.slice(0, MAX_NEURONS_PER_LAYER);
      const activeEventIds = new Set(
        (snapshot?.collapse_events ?? [])
          .filter((event) => event.status === "active" && event.layer_id === layer.layer_id)
          .map((event) => event.neuron_id),
      );
      if (selectedNeuronId && ids.includes(selectedNeuronId) && !displayed.includes(selectedNeuronId)) {
        displayed[displayed.length - 1] = selectedNeuronId;
      }
      result.push({
        id: `layer-${layer.layer_id}`,
        type: "card",
        position: { x: layerIndex * 245, y: 0 },
        data: {
          kind: "layer",
          label: `${layer.layer_id} · ${layer.neuron_count}`,
          layerId: layer.layer_id,
        },
        draggable: false,
        selectable: false,
        style: { width: 205, height: 34, border: 0, background: "transparent" },
      });
      displayed.forEach((neuronId, neuronIndex) => {
        const row = snapshot?.live_neuron_buffers[neuronId]?.at(-1);
        const metrics = row?.metrics;
        const state = activeEventIds.has(neuronId)
          ? "anomaly"
          : metrics && usableMetric(metrics.inactive_fraction) && metrics.inactive_fraction === 1
            ? "inactive"
            : metrics
              ? "active"
              : "unavailable";
        const hint =
          state === "anomaly"
            ? "COLLAPSE EVENT"
            : state === "inactive"
              ? "INACTIVE"
              : row
                ? `EPOCH ${row.epoch}`
                : "NO METRIC";
        result.push({
          id: `neuron-${layer.layer_id}-${neuronId}`,
          type: "card",
          position: { x: layerIndex * 245 + 4, y: 44 + neuronIndex * 48 },
          data: { kind: "neuron", label: neuronId, layerId: layer.layer_id, state, hint },
          selected: selectedNeuronId === neuronId,
          draggable: false,
          style: { width: 195, border: 0, background: "transparent", padding: 0 },
        });
      });
    });
    return result;
  }, [architecture, selectedNeuronId, snapshot]);

  const liveLabel =
    socketStatus === "connected"
      ? snapshot && snapshot.current_epoch >= 0
        ? "Live stream connected"
        : "Connected · awaiting metrics"
      : snapshot
        ? "Cached snapshot · stale"
        : "Live data unavailable";

  async function handleRefresh() {
    setSuppressStream(false);
    await refreshProjects();
    await Promise.all([
      refreshExperiments(),
      selectedId && selectedProjectId
        ? refreshSelected(selectedId, selectedProjectId)
        : Promise.resolve(),
    ]);
  }

  async function handleClear() {
    if (!selectedId || busy) return;
    const confirmed = window.confirm(
      "Clear this experiment's active in-memory state? Live metrics, architecture registration, and collapse events will be removed from the backend process. Persistent project and experiment metadata will remain. The SDK must resume/register again to send new data.",
    );
    if (!confirmed) return;
    setBusy(true);
    setDetailError("");
    try {
      if (!selectedProjectId) return;
      await api.clearLiveState(selectedId, selectedProjectId);
      setSnapshot(null);
      setSuppressStream(true);
      socketRef.current?.close();
      await Promise.all([
        refreshExperiments(),
        refreshSelected(selectedId, selectedProjectId),
      ]);
      setNotice("Live in-memory state cleared. Persistent metadata remains.");
    } catch (error) {
      setDetailError(`Cleanup failed: ${errorText(error)}`);
    } finally {
      setBusy(false);
    }
  }

  const metricAvailability = (metric: keyof NeuronMetrics) => {
    const value = latestRow?.metrics?.[metric];
    return usableMetric(value) ? formatValue(value) : "Unavailable";
  };

  async function handleAuthSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAuthError("");
    setAuthNotice("");
    try {
      if (authMode === "register") {
        await api.register(authEmail, authPassword);
        setAuthMode("login");
        setAuthNotice("Check your email for a verification link before signing in.");
      } else if (authMode === "reset" && resetToken) {
        await api.resetPassword(resetToken, newPassword);
        setResetToken("");
        setAuthMode("login");
        setAuthNotice("Password updated. Sign in with your new password.");
        window.history.replaceState({}, "", window.location.pathname);
      } else if (authMode === "reset") {
        await api.requestPasswordReset(authEmail);
        setAuthMode("login");
        setAuthNotice("If the account exists, a password-reset link has been sent.");
      } else {
        const result = await api.login(authEmail, authPassword);
        setUser(result.user);
        setAuthPassword("");
        setAuthNotice("");
        setCanResendVerification(false);
      }
    } catch (error) {
      setAuthError(errorText(error));
      setCanResendVerification(
        authMode === "login" &&
        error instanceof Error &&
        "status" in error &&
        error.status === 403,
      );
    }
  }

  async function handleResendVerification() {
    setAuthError("");
    try {
      await api.resendVerification(authEmail, authPassword);
      setAuthNotice("If this account is unverified, a new verification link has been sent.");
      setCanResendVerification(false);
    } catch (error) {
      setAuthError(errorText(error));
    }
  }

  async function handleLogout() {
    try {
      await api.logout();
      setUser(null);
      setAuthPassword("");
      setSelectedId("");
    } catch (error) {
      setAuthError(`Could not sign out: ${errorText(error)}`);
    }
  }

  async function handleDeviceApproval() {
    setAuthError("");
    try {
      await api.approveDevice(deviceCode);
      setAuthNotice("SDK authorization approved. Return to your training process.");
      setDeviceCode("");
      const params = new URLSearchParams(window.location.search);
      params.delete("device_code");
      window.history.replaceState({}, "", `${window.location.pathname}${params.size ? `?${params}` : ""}`);
    } catch (error) {
      setAuthError(errorText(error));
    }
  }

  async function handleClaimProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setClaimError("");
    setClaimBusy(true);
    try {
      await api.claimProject(legacyProjectKey);
      setLegacyProjectKey("");
      await refreshProjects();
      setNotice("Legacy project claimed and added to your workspace.");
    } catch (error) {
      setClaimError(errorText(error));
    } finally {
      setClaimBusy(false);
    }
  }

  async function handleCreateProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setProjectError("");
    setProjectBusy(true);
    try {
      const created = await api.createProject(projectName);
      setProjects((current) => [
        created,
        ...current.filter((item) => item.project_id !== created.project_id),
      ]);
      setProjectName("");
      setSelectedProjectId(created.project_id);
      setSelectedId("");
      setNotice("Project created. Issue a credential for Kaggle or the SDK.");
    } catch (error) {
      setProjectError(errorText(error));
    } finally {
      setProjectBusy(false);
    }
  }

  async function handleRenameProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProjectId) return;
    setProjectError("");
    setProjectBusy(true);
    try {
      const updated = await api.renameProject(selectedProjectId, renameProjectName);
      setProjects((current) =>
        current.map((item) =>
          item.project_id === updated.project_id ? updated : item,
        ),
      );
      setNotice("Project renamed.");
    } catch (error) {
      setProjectError(errorText(error));
    } finally {
      setProjectBusy(false);
    }
  }

  async function handleArchiveProject() {
    if (!selectedProjectId || projectBusy) return;
    const confirmed = window.confirm(
      "Archive this project? Its API credentials will be revoked and active in-memory monitoring state will be cleared. Project and experiment metadata remain in the database, but the project will no longer appear in your workspace.",
    );
    if (!confirmed) return;
    setProjectError("");
    setProjectBusy(true);
    try {
      await api.archiveProject(selectedProjectId);
      const remaining = projects.filter(
        (project) => project.project_id !== selectedProjectId,
      );
      setProjects(remaining);
      setSelectedProjectId(remaining[0]?.project_id ?? "");
      setSelectedId("");
      setNotice("Project archived; persistent metadata was retained.");
    } catch (error) {
      setProjectError(errorText(error));
    } finally {
      setProjectBusy(false);
    }
  }

  async function handleCreateCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedProjectId) return;
    setCredentialError("");
    setCopyError("");
    setCredentialBusy(true);
    try {
      const issued = await api.createProjectCredential(
        selectedProjectId,
        credentialName,
      );
      setIssuedCredential(issued);
      await refreshProjectCredentials(selectedProjectId);
    } catch (error) {
      setCredentialError(errorText(error));
    } finally {
      setCredentialBusy(false);
    }
  }

  async function handleRotateCredential(credential: ProjectCredential) {
    if (!selectedProjectId || credentialBusy || credential.revoked_at) return;
    if (
      !window.confirm(
        `Rotate "${credential.name}"? The existing credential will stop working immediately.`,
      )
    ) return;
    setCredentialError("");
    setCopyError("");
    setCredentialBusy(true);
    try {
      const issued = await api.rotateProjectCredential(
        selectedProjectId,
        credential.credential_id,
        credential.name === "Legacy project key" ? credentialName : credential.name,
      );
      setIssuedCredential(issued);
      await refreshProjectCredentials(selectedProjectId);
    } catch (error) {
      setCredentialError(errorText(error));
    } finally {
      setCredentialBusy(false);
    }
  }

  async function handleRevokeCredential(credential: ProjectCredential) {
    if (!selectedProjectId || credentialBusy || credential.revoked_at) return;
    if (
      !window.confirm(
        `Revoke "${credential.name}"? Training jobs using it will no longer authenticate.`,
      )
    ) return;
    setCredentialError("");
    setCredentialBusy(true);
    try {
      await api.revokeProjectCredential(
        selectedProjectId,
        credential.credential_id,
      );
      setIssuedCredential(null);
      await refreshProjectCredentials(selectedProjectId);
    } catch (error) {
      setCredentialError(errorText(error));
    } finally {
      setCredentialBusy(false);
    }
  }

  async function handleCopyCredential() {
    if (!issuedCredential) return;
    try {
      await navigator.clipboard.writeText(issuedCredential.api_key);
      setCopyError("");
      setNotice("Credential copied. Store it in Kaggle Secrets; it cannot be shown again.");
    } catch (error) {
      setCopyError(`Clipboard access failed: ${errorText(error)}`);
    }
  }

  if (authLoading) {
    return <main className="auth-shell" aria-label="Loading dashboard session">Checking dashboard session…</main>;
  }

  if (!user) {
    return (
      <main className="auth-shell">
        <form className="auth-card" onSubmit={(event) => void handleAuthSubmit(event)}>
          <div className="auth-brand"><span className="brand-mark">N</span><strong>Neuron<span className="brand-light">Scope</span></strong></div>
          <p className="eyebrow">PRIVATE MONITORING WORKSPACE</p>
          <h1>{authMode === "register" ? "Create your account" : authMode === "reset" ? resetToken ? "Choose a new password" : "Reset your password" : "Sign in"}</h1>
          <p className="auth-description">Your projects and experiment data are available only after signing in.</p>
          {authNotice && <div className="auth-message success">{authNotice}</div>}
          {authError && <div className="auth-message error" role="alert">{authError}</div>}
          {authMode !== "reset" || !resetToken ? (
            <label>Email address<input type="email" autoComplete="email" required value={authEmail} onChange={(event) => setAuthEmail(event.target.value)} /></label>
          ) : null}
          {authMode === "login" || authMode === "register" ? (
            <label>Password<input type="password" autoComplete={authMode === "login" ? "current-password" : "new-password"} required minLength={authMode === "register" ? 12 : 1} value={authPassword} onChange={(event) => setAuthPassword(event.target.value)} /></label>
          ) : null}
          {authMode === "reset" && resetToken ? (
            <label>New password<input type="password" autoComplete="new-password" required minLength={12} value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /></label>
          ) : null}
          <button className="button primary auth-submit" type="submit">
            {authMode === "register" ? "Create account" : authMode === "reset" ? resetToken ? "Update password" : "Send reset link" : "Sign in"}
          </button>
          <div className="auth-links">
            {authMode === "login" && <>
              <button type="button" className="text-button" onClick={() => { setAuthMode("register"); setAuthError(""); }}>Create an account</button>
              <button type="button" className="text-button" onClick={() => { setAuthMode("reset"); setAuthError(""); }}>Forgot password?</button>
            </>}
            {authMode !== "login" && !resetToken && <button type="button" className="text-button" onClick={() => { setAuthMode("login"); setAuthError(""); }}>Back to sign in</button>}
          </div>
          {canResendVerification && (
            <button type="button" className="text-button" onClick={() => void handleResendVerification()}>
              Resend verification email
            </button>
          )}
        </form>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <a className="brand" href="#" onClick={(event) => { event.preventDefault(); setSelectedId(""); }}>
          <span className="brand-mark">N</span>
          <span>Neuron<span className="brand-light">Scope</span></span>
        </a>
        <div className="topbar-right">
          <div className={`connection-pill ${socketStatus}`}>
            <span className="connection-dot" />
            {selectedId ? socketStatus : "dashboard"}
          </div>
          <div className="project-chip">
            <span className="project-glyph">◆</span>
            <span>{projects.find((project) => project.project_id === selectedProjectId)?.name ?? "No project selected"}</span>
          </div>
          <button className="icon-button" onClick={() => void handleRefresh()} title="Refresh dashboard" aria-label="Refresh dashboard">
            ↻
          </button>
          <button className="text-button" onClick={() => void handleLogout()}>Sign out</button>
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <div className="sidebar-heading">
            <div>
              <p className="eyebrow">WORKSPACE</p>
              <h1>Experiments</h1>
            </div>
            <span className="count-pill">{experiments.length}</span>
          </div>
          <div className="project-card">
            <div className="project-avatar">P</div>
            <div className="project-card-copy">
              <span className="muted-label">PROJECTS · {projects.length}</span>
              {projects.length ? (
                <select
                  aria-label="Select project"
                  value={selectedProjectId}
                  onChange={(event) => {
                    setSelectedProjectId(event.target.value);
                    setSelectedId("");
                  }}
                >
                  {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
                </select>
              ) : <strong>No projects</strong>}
            </div>
            <span className="online-indicator" title="Signed in" />
          </div>
          <form className="project-create" onSubmit={(event) => void handleCreateProject(event)}>
            <label htmlFor="new-project-name">Create a project</label>
            <input
              id="new-project-name"
              maxLength={160}
              required
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="Project name"
            />
            <button className="button secondary" type="submit" disabled={projectBusy}>
              {projectBusy ? "Creating…" : "Create project"}
            </button>
          </form>
          {selectedProjectId && (
            <details className="project-settings">
              <summary>Project settings & credentials</summary>
              <div className="project-identity">
                <span>Project ID</span>
                <code>{selectedProjectId}</code>
                <span>Created</span>
                <code>
                  {formatDate(
                    projects.find((item) => item.project_id === selectedProjectId)
                      ?.created_at,
                  )}
                </code>
              </div>
              <form className="project-rename" onSubmit={(event) => void handleRenameProject(event)}>
                <label htmlFor="rename-project-name">Rename project</label>
                <input
                  id="rename-project-name"
                  maxLength={160}
                  required
                  value={renameProjectName}
                  onChange={(event) => setRenameProjectName(event.target.value)}
                />
                <button className="button secondary" type="submit" disabled={projectBusy}>
                  Rename
                </button>
              </form>
              <button
                className="button danger project-archive"
                type="button"
                onClick={() => void handleArchiveProject()}
                disabled={projectBusy}
              >
                Archive project
              </button>
              <div className="credential-manager">
                <strong>SDK credentials</strong>
                <span className="muted-copy">
                  Create a dedicated credential and save it once in Kaggle Secrets.
                  Raw credentials are never shown again.
                </span>
                <form onSubmit={(event) => void handleCreateCredential(event)}>
                  <label htmlFor="credential-name">Credential name</label>
                  <input
                    id="credential-name"
                    maxLength={100}
                    required
                    value={credentialName}
                    onChange={(event) => setCredentialName(event.target.value)}
                  />
                  <button className="button primary" type="submit" disabled={credentialBusy}>
                    {credentialBusy ? "Issuing…" : "Generate credential"}
                  </button>
                </form>
                {issuedCredential && (
                  <div className="issued-credential" role="status">
                    <strong>Copy this credential now</strong>
                    <code>{issuedCredential.api_key}</code>
                    <span>Project ID: <code>{issuedCredential.project_id}</code></span>
                    <div className="credential-actions">
                      <button className="button secondary" type="button" onClick={() => void handleCopyCredential()}>
                        Copy credential
                      </button>
                      <button className="text-button" type="button" onClick={() => setIssuedCredential(null)}>
                        Dismiss
                      </button>
                    </div>
                    {copyError && <span className="auth-message error">{copyError}</span>}
                  </div>
                )}
                {credentialError && <div className="auth-message error">{credentialError}</div>}
                {credentialsLoading ? (
                  <span className="muted-copy">Loading credentials…</span>
                ) : (
                  <ul className="credential-list">
                    {credentials.map((credential) => (
                      <li key={credential.credential_id}>
                        <div>
                          <strong>{credential.name}</strong>
                          <small>
                            {credential.revoked_at
                              ? `Revoked ${formatDate(credential.revoked_at)}`
                              : `Active · created ${formatDate(credential.created_at)}`}
                          </small>
                        </div>
                        {!credential.revoked_at && (
                          <div className="credential-actions">
                            <button
                              className="text-button"
                              type="button"
                              disabled={credentialBusy}
                              onClick={() => void handleRotateCredential(credential)}
                            >
                              Rotate
                            </button>
                            <button
                              className="text-button danger-text"
                              type="button"
                              disabled={credentialBusy}
                              onClick={() => void handleRevokeCredential(credential)}
                            >
                              Revoke
                            </button>
                          </div>
                        )}
                      </li>
                    ))}
                    {!credentials.length && !credentialError && (
                      <li className="muted-copy">No SDK credentials have been issued.</li>
                    )}
                  </ul>
                )}
              </div>
              {projectError && <div className="auth-message error">{projectError}</div>}
            </details>
          )}
          <details className="claim-project">
            <summary>Claim a legacy project</summary>
            <form onSubmit={(event) => void handleClaimProject(event)}>
              <label>Existing project API key
                <input
                  type="password"
                  autoComplete="off"
                  required
                  value={legacyProjectKey}
                  onChange={(event) => setLegacyProjectKey(event.target.value)}
                />
              </label>
              <button className="button secondary" type="submit" disabled={claimBusy}>
                {claimBusy ? "Claiming…" : "Claim project"}
              </button>
              {claimError && <span className="auth-message error">{claimError}</span>}
            </form>
          </details>

          {projectsError && <div className="inline-notice error-notice">{projectsError}</div>}
          {projects.length === 0 && !projectsError ? (
            <div className="sidebar-empty">
              <span className="empty-icon">◌</span>
              <strong>No projects yet</strong>
              <span>Initialize the SDK and approve its device authorization to create a project.</span>
            </div>
          ) : listLoading ? (
            <div className="loading-list" aria-label="Loading experiments">
              <span /><span /><span />
            </div>
          ) : listError ? (
            <div className="inline-notice error-notice">
              <strong>Could not load experiments</strong>
              <span>{listError}</span>
              <button className="text-button" onClick={() => void refreshExperiments()}>Try again</button>
            </div>
          ) : experiments.length === 0 ? (
            <div className="sidebar-empty">
              <span className="empty-icon">◌</span>
              <strong>No experiments yet</strong>
              <span>Start a run with the NeuronScope SDK to see it here.</span>
            </div>
          ) : (
            <nav className="experiment-list" aria-label="Experiments">
              {experiments.map((experiment) => (
                <button
                  className={`experiment-item ${selectedId === experiment.experiment_id ? "selected" : ""}`}
                  key={experiment.experiment_id}
                  onClick={() => setSelectedId(experiment.experiment_id)}
                >
                  <span className={`experiment-status ${statusClass(experiment.status)}`} />
                  <span className="experiment-item-copy">
                    <strong>{experiment.name}</strong>
                    <small>{experiment.framework} <span>·</span> {experiment.status}</small>
                  </span>
                  <span className="experiment-epoch">
                    {experiment.latest_epoch >= 0 ? `E${experiment.latest_epoch}` : "—"}
                  </span>
                </button>
              ))}
            </nav>
          )}

          <div className="sidebar-footer">
            <span className="footer-pulse" />
            Backend is the source of truth
          </div>
        </aside>

        <section className="main-content">
          {deviceCode && (
            <div className="device-approval">
              <div><strong>Approve SDK device</strong><span>Enter this dashboard only if you initiated SDK setup. Code: <code>{deviceCode}</code></span></div>
              <button className="button primary" onClick={() => void handleDeviceApproval()}>Approve</button>
              <button className="text-button" onClick={() => setDeviceCode("")}>Dismiss</button>
              {authError && <span className="auth-message error">{authError}</span>}
            </div>
          )}
          {!selectedId ? (
            <div className="welcome-panel">
              <div className="welcome-orbit"><span /><span /><span /></div>
              <p className="eyebrow">NEURON-LEVEL OBSERVABILITY</p>
              <h2>Training, made visible.</h2>
              <p>Select an experiment to inspect its architecture, live neuron metrics, and collapse-event evidence.</p>
              {listError && <div className="inline-notice error-notice">{listError}</div>}
            </div>
          ) : (
            <>
              <div className="page-heading">
                <div>
                  <div className="breadcrumb"><span>Experiments</span><i>/</i><span>{detail?.name ?? "Loading experiment"}</span></div>
                  <div className="title-line">
                    <h2>{detail?.name ?? "Loading experiment…"}</h2>
                    {detail && <span className={`status-badge ${statusClass(detail.status)}`}><span />{detail.status}</span>}
                  </div>
                  <p className="subheading">
                    {detail?.model_name ?? "Model not reported"}
                    <span className="bullet">·</span>{detail?.framework ?? "—"}
                    <span className="bullet">·</span><code>{selectedId}</code>
                  </p>
                </div>
                <div className="heading-actions">
                  <button className="button secondary" onClick={() => void handleRefresh()} disabled={busy}>
                    <span className="button-icon">↻</span> Refresh
                  </button>
                  <button className="button danger" onClick={() => void handleClear()} disabled={busy || !snapshot}>
                    <span className="button-icon">⌫</span> {busy ? "Clearing…" : "Clear live state"}
                  </button>
                </div>
              </div>

              {detailError && <div className="banner error-banner"><span>!</span>{detailError}</div>}
              {socketError && <div className="banner warning-banner"><span>◷</span>{socketError}</div>}

              <div className="summary-grid">
                <div className="summary-card">
                  <span className="summary-icon purple">◫</span>
                  <div><span className="summary-label">CURRENT EPOCH</span><strong>{snapshot && snapshot.current_epoch >= 0 ? snapshot.current_epoch : "—"}</strong></div>
                  <small>{snapshot?.epochs.length ?? 0} retained</small>
                </div>
                <div className="summary-card">
                  <span className="summary-icon blue">⌘</span>
                  <div><span className="summary-label">ARCHITECTURE</span><strong>{layers.length ? `${layers.length} layers` : "Unavailable"}</strong></div>
                  <small>{layers.reduce((sum, layer) => sum + layer.neuron_count, 0).toLocaleString()} units declared</small>
                </div>
                <div className="summary-card">
                  <span className="summary-icon green">◉</span>
                  <div><span className="summary-label">LIVE MONITORING</span><strong>{liveLabel}</strong></div>
                  <small>{snapshot?.last_activity ? `Backend activity ${formatDate(snapshot.last_activity)}` : "No live activity recorded"}</small>
                </div>
                <div className="summary-card">
                  <span className="summary-icon orange">⌁</span>
                  <div><span className="summary-label">COLLAPSE EVENTS</span><strong>{activeEvents.length} active</strong></div>
                  <small>{snapshot?.collapse_events.length ?? 0} preserved events</small>
                </div>
              </div>

              {!snapshot && !detailError ? (
                <div className="loading-panel"><span className="spinner" /> Loading backend state…</div>
              ) : snapshot && !architecture ? (
                <div className="empty-card">
                  <span className="empty-icon">⌘</span>
                  <h3>Architecture is not registered</h3>
                  <p>Experiment metadata exists, but the live backend has no registered model architecture. Resume the run with the SDK to register it.</p>
                </div>
              ) : snapshot && architecture ? (
                <>
                  <section className="panel graph-panel">
                    <div className="panel-heading">
                      <div><p className="eyebrow">MODEL TOPOLOGY</p><h3>Network architecture</h3></div>
                      <div className="graph-legend">
                        <span><i className="legend-dot normal" />Observed</span>
                        <span><i className="legend-dot inactive" />Inactive</span>
                        <span><i className="legend-dot anomaly" />Collapse event</span>
                        <span><i className="legend-dot unknown" />Unavailable</span>
                      </div>
                    </div>
                    <div className="graph-meta">
                      <span>{architecture.model_name} <b>·</b> {architecture.framework}</span>
                      <span>No connections are inferred; the backend architecture provides layer and neuron membership only.</span>
                    </div>
                    <div className="flow-wrap">
                      <ReactFlow<FlowNode>
                        nodes={flowNodes}
                        edges={[]}
                        nodeTypes={nodeTypes}
                        fitView
                        fitViewOptions={{ padding: 0.18 }}
                        minZoom={0.15}
                        maxZoom={1.5}
                        nodesConnectable={false}
                        nodesDraggable={false}
                        elementsSelectable={false}
                        onNodeClick={(_, node) => {
                          if (node.data.kind === "layer") {
                            setSelectedLayerId(node.data.layerId);
                            setSelectedNeuronId("");
                          } else {
                            setSelectedLayerId(node.data.layerId);
                            setSelectedNeuronId(node.data.label);
                          }
                        }}
                      >
                        <Background color="#253044" gap={24} size={1} />
                        <Controls showInteractive={false} />
                        <MiniMap pannable zoomable nodeColor={(node) => {
                          const state = (node.data as FlowData).state;
                          return state === "anomaly" ? "#f27c72" : state === "inactive" ? "#efb35f" : "#596b87";
                        }} />
                      </ReactFlow>
                      <div className="graph-caption">Showing up to {MAX_NEURONS_PER_LAYER} neurons per layer. Use the selector below to inspect any declared neuron.</div>
                    </div>
                    <div className="selection-bar">
                      <label>
                        <span>Layer</span>
                        <select
                          value={selectedLayer?.layer_id ?? ""}
                          onChange={(event) => {
                            setSelectedLayerId(event.target.value);
                            setSelectedNeuronId("");
                          }}
                        >
                          {layers.map((layer) => <option value={layer.layer_id} key={layer.layer_id}>{layer.layer_id} · {layer.type}</option>)}
                        </select>
                      </label>
                      <label>
                        <span>Neuron / channel</span>
                        <select
                          value={selectedNeuronId}
                          onChange={(event) => setSelectedNeuronId(event.target.value)}
                          disabled={!layerNeuronIds.length}
                        >
                          {layerNeuronIds.map((id) => <option value={id} key={id}>{id}</option>)}
                        </select>
                      </label>
                      <span className="selection-detail">
                        {selectedLayer ? `${selectedLayer.neuron_count.toLocaleString()} declared units` : "No layer selected"}
                        {lastMessageAt && socketStatus === "connected" && <span> · Updated {new Date(lastMessageAt).toLocaleTimeString()}</span>}
                      </span>
                    </div>
                  </section>

                  <div className="monitor-grid">
                    <section className="panel chart-panel">
                      <div className="panel-heading">
                        <div><p className="eyebrow">FIVE-EPOCH WINDOW</p><h3>Neuron metrics</h3></div>
                        <select className="metric-select" value={metricKey} onChange={(event) => setMetricKey(event.target.value as typeof metricKey)}>
                          {METRICS.map((metric) => <option value={metric.key} key={metric.key}>{metric.label}</option>)}
                        </select>
                      </div>
                      <div className="selected-neuron-line">
                        <span className={`experiment-status ${latestRow?.metrics ? "running" : "neutral"}`} />
                        <strong>{selectedNeuronId || "Select a neuron"}</strong>
                        <span>{selectedLayer?.layer_id ?? ""}</span>
                        <span className="value-chip">{selectedMetric.label}: {formatValue(latestRow?.metrics?.[metricKey])}</span>
                      </div>
                      {selectedRows.length ? (
                        <Suspense fallback={<div className="chart-empty"><span className="spinner" /> Loading chart…</div>}>
                          <MetricPlot
                            data={chartData}
                            layout={{
                              autosize: true,
                              height: 228,
                              margin: { l: 42, r: 12, t: 12, b: 34 },
                              paper_bgcolor: "transparent",
                              plot_bgcolor: "transparent",
                              font: { color: "#8692a8", family: "Inter, sans-serif", size: 11 },
                              xaxis: { title: { text: "Epoch" }, gridcolor: "#202a3a", zerolinecolor: "#283245", dtick: 1 },
                              yaxis: { gridcolor: "#202a3a", zerolinecolor: "#283245", automargin: true },
                              showlegend: false,
                              hovermode: "x unified",
                            }}
                          />
                        </Suspense>
                      ) : (
                        <div className="chart-empty">
                          <span className="chart-empty-icon">⌁</span>
                          <strong>{snapshot && snapshot.current_epoch < 0 ? "Waiting for the first epoch" : "No metric observations for this neuron"}</strong>
                          <span>Missing measurements remain unavailable; they are not treated as zero.</span>
                        </div>
                      )}
                      <div className="metric-strip">
                        <div><span>Activation mean</span><strong>{metricAvailability("activation_mean")}</strong></div>
                        <div><span>Gradient mean</span><strong>{metricAvailability("gradient_mean")}</strong></div>
                        <div><span>Weight norm</span><strong>{metricAvailability("weight_norm")}</strong></div>
                        <div><span>Samples</span><strong>{latestRow?.metrics?.sample_count ?? "—"}</strong></div>
                      </div>
                    </section>

                    <section className="panel event-panel">
                      <div className="panel-heading">
                        <div><p className="eyebrow">DETECTED BEHAVIOR</p><h3>Collapse events</h3></div>
                        <span className="count-pill">{snapshot.collapse_events.length}</span>
                      </div>
                      {!snapshot.collapse_events.length ? (
                        <div className="events-empty">
                          <span className="event-empty-icon">✓</span>
                          <strong>No collapse events</strong>
                          <span>New detections and recoveries will appear here.</span>
                        </div>
                      ) : (
                        <div className="event-list">
                          {[...snapshot.collapse_events].sort((a, b) => b.detected_epoch - a.detected_epoch).map((event) => (
                            <EventCard key={event.event_id} event={event} architecture={architecture} />
                          ))}
                        </div>
                      )}
                    </section>
                  </div>
                </>
              ) : null}
            </>
          )}
        </section>
      </div>
      {notice && <div className="toast" role="status">{notice}</div>}
    </main>
  );
}

function EventCard({
  event,
  architecture,
}: {
  event: CollapseEvent;
  architecture: LiveSnapshot["architecture"];
}) {
  const [expanded, setExpanded] = useState(false);
  const classNames = (event.affected_classes ?? []).map(
    (index) => architecture?.classes[String(index)] ?? `Class ${index}`,
  );
  return (
    <article className={`event-card ${event.status === "recovered" ? "recovered" : ""}`}>
      <button className="event-summary" onClick={() => setExpanded((value) => !value)} aria-expanded={expanded}>
        <span className={`event-indicator ${event.status === "recovered" ? "recovered" : ""}`}>{event.status === "recovered" ? "✓" : "!"}</span>
        <span className="event-summary-copy">
          <strong>{event.event_type.replaceAll("_", " ")}</strong>
          <small>{event.layer_id} <span>·</span> {event.neuron_id}</small>
        </span>
        <span className={`status-badge small ${statusClass(event.status)}`}><span />{event.status}</span>
      </button>
      <div className="event-meta">
        <span>Epoch {event.detected_epoch}</span>
        <span>{event.created_at ? formatDate(event.created_at) : "Timestamp unavailable"}</span>
      </div>
      {classNames.length > 0 && <div className="event-classes">{classNames.map((name) => <span key={name}>{name}</span>)}</div>}
      {event.status === "recovered" && (
        <div className="recovery-line">
          Recovered{event.recovered_epoch != null ? ` at epoch ${event.recovered_epoch}` : ""}
          {typeof event.recovery?.metric === "string" && ` · ${event.recovery.metric}`}
          {usableMetric(event.recovery?.value) && ` ${formatValue(event.recovery.value)}`}
        </div>
      )}
      {expanded && (
        <div className="evidence-panel">
          <div className="evidence-title"><strong>Independent event evidence</strong><span>Captured at detection · not the rolling live window</span></div>
          {event.evidence
            ? <pre>{JSON.stringify(event.evidence, null, 2)}</pre>
            : <span className="muted-copy">No evidence snapshot was provided.</span>}
          <span className="event-rule">Rule version: {event.rule_version || "—"}</span>
        </div>
      )}
    </article>
  );
}
