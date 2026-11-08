import type {
  CollapseEvent,
  LiveSnapshot,
  NeuronEpoch,
  NeuronMetrics,
} from "./types";

export const METRIC_WINDOW = 5;

export function usableMetric(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function metricValue(
  epoch: NeuronEpoch | undefined,
  metric: string,
): number | null {
  const value = epoch?.metrics?.[metric];
  return usableMetric(value) ? value : null;
}

export function visibleNeuronIds(
  layerId: string,
  count: number,
  explicitIds: string[] | null | undefined,
  buffers: LiveSnapshot["live_neuron_buffers"],
): string[] {
  if (explicitIds?.length) return explicitIds;
  const observed = Object.entries(buffers)
    .filter(([, rows]) => rows.some((row) => row.metrics?.layer_id === layerId))
    .map(([id]) => id);
  if (observed.length) return observed;
  return Array.from({ length: count }, (_, index) => `${layerId}:${index}`);
}

export function applySocketMessage(
  snapshot: LiveSnapshot,
  message: Record<string, unknown>,
): LiveSnapshot {
  if (message.type === "snapshot") return message as unknown as LiveSnapshot;
  if (message.type === "epoch_update") {
    const epoch = message.epoch;
    const incoming = message.neurons;
    if (
      typeof epoch !== "number" ||
      !incoming ||
      typeof incoming !== "object"
    ) {
      return snapshot;
    }
    const updates = incoming as Record<
      string,
      { layer_id?: string; metrics?: NeuronMetrics }
    >;
    const buffers = { ...snapshot.live_neuron_buffers };
    const knownIds = new Set([
      ...Object.keys(buffers),
      ...Object.keys(updates),
      ...(snapshot.architecture?.layers.flatMap((layer) =>
        visibleNeuronIds(
          layer.layer_id,
          layer.neuron_count,
          layer.neuron_ids,
          buffers,
        ),
      ) ?? []),
    ]);
    for (const id of knownIds) {
      const update = updates[id];
      const previous = buffers[id] ?? [];
      const row: NeuronEpoch = {
        epoch,
        metrics: update?.metrics
          ? { ...update.metrics, layer_id: update.layer_id ?? update.metrics.layer_id }
          : null,
      };
      buffers[id] = [...previous.filter((item) => item.epoch !== epoch), row]
        .sort((a, b) => a.epoch - b.epoch)
        .slice(-METRIC_WINDOW);
    }
    const epochs = [...new Set([...snapshot.epochs, epoch])]
      .sort((a, b) => a - b)
      .slice(-METRIC_WINDOW);
    return {
      ...snapshot,
      revision:
        typeof message.revision === "number"
          ? message.revision
          : snapshot.revision + 1,
      current_epoch: epoch,
      epochs,
      live_neuron_buffers: buffers,
    };
  }
  if (
    message.type === "collapse_event" ||
    message.type === "collapse_recovered"
  ) {
    const event = message.event as CollapseEvent | undefined;
    if (!event?.event_id) return snapshot;
    const events = snapshot.collapse_events.filter(
      (item) => item.event_id !== event.event_id,
    );
    return {
      ...snapshot,
      revision:
        typeof message.revision === "number"
          ? message.revision
          : snapshot.revision + 1,
      collapse_events: [...events, event],
    };
  }
  if (message.type === "experiment_finished") {
    return { ...snapshot, status: "finished" };
  }
  return snapshot;
}
