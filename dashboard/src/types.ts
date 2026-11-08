export type MetricValue = number | null;

export interface Layer {
  layer_id: string;
  type: string;
  neuron_count: number;
  neuron_ids?: string[] | null;
}

export interface Architecture {
  version: number;
  framework: string;
  model_name: string;
  classes: Record<string, string>;
  layers: Layer[];
}

export interface NeuronMetrics {
  layer_id: string;
  activation_mean?: MetricValue;
  activation_variance?: MetricValue;
  activation_quantiles?: Record<string, MetricValue> | null;
  inactive_fraction?: MetricValue;
  gradient_mean?: MetricValue;
  gradient_variance?: MetricValue;
  weight_norm?: MetricValue;
  sample_count?: number | null;
  uncertainty?: MetricValue;
  class_contribution?: MetricValue[] | null;
  class_sample_counts?: (number | null)[] | null;
  class_validity_mask?: boolean[] | null;
  class_contribution_uncertainty?: MetricValue[] | null;
  [key: string]: unknown;
}

export interface NeuronEpoch {
  epoch: number;
  metrics: NeuronMetrics | null;
}

export interface CollapseEvent {
  event_id: string;
  neuron_id: string;
  layer_id: string;
  event_type: string;
  affected_classes: number[];
  detected_epoch: number;
  evidence: Record<string, unknown> | null;
  rule_version: string;
  status: "active" | "recovered" | string;
  recovery: Record<string, unknown> | null;
  created_at?: string;
  updated_at?: string;
  recovered_epoch?: number | null;
}

export interface LiveSnapshot {
  type: "snapshot";
  experiment_id: string;
  name: string;
  framework: string;
  model_name: string | null;
  status: string;
  revision: number;
  last_activity: string | null;
  architecture: Architecture | null;
  current_epoch: number;
  epochs: number[];
  pending_epoch?: {
    epoch: number;
    received_chunks: number;
    chunk_count: number;
  } | null;
  live_neuron_buffers: Record<string, NeuronEpoch[]>;
  collapse_events: CollapseEvent[];
}

export interface ExperimentSummary {
  experiment_id: string;
  name: string;
  status: string;
  latest_epoch: number;
  framework: string;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
  description: string | null;
  created_at: string;
}

export interface ProjectCredential {
  credential_id: string;
  name: string;
  created_at: string;
  revoked_at: string | null;
  is_legacy: boolean;
}

export interface IssuedProjectCredential extends ProjectCredential {
  project_id: string;
  api_key: string;
}

export interface ExperimentDetail {
  experiment_id: string;
  project_id: string;
  name: string;
  framework: string;
  model_name: string | null;
  status: string;
  current_epoch: number;
  architecture_registered: boolean;
  last_activity: string | null;
}

export type SocketStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "disconnected";
