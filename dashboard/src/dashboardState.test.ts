import { describe, expect, it } from "vitest";
import {
  applySocketMessage,
  metricValue,
  visibleNeuronIds,
} from "./dashboardState";
import type { LiveSnapshot } from "./types";

const initial: LiveSnapshot = {
  type: "snapshot",
  experiment_id: "exp-1",
  name: "test",
  framework: "pytorch",
  model_name: "MLP",
  status: "running",
  revision: 1,
  last_activity: null,
  architecture: {
    version: 1,
    framework: "pytorch",
    model_name: "MLP",
    classes: { "0": "cat" },
    layers: [{ layer_id: "hidden", type: "Linear", neuron_count: 2, neuron_ids: ["a", "b"] }],
  },
  current_epoch: 4,
  epochs: [0, 1, 2, 3, 4],
  live_neuron_buffers: {
    a: [{ epoch: 4, metrics: { layer_id: "hidden", activation_mean: 0.4 } }],
  },
  collapse_events: [],
};

describe("dashboard live state", () => {
  it("keeps the rolling window to five epochs and preserves missing measurements", () => {
    const updated = applySocketMessage(initial, {
      type: "epoch_update",
      epoch: 5,
      revision: 2,
      neurons: { a: { layer_id: "hidden", metrics: { layer_id: "hidden", activation_mean: 0.6 } } },
    });
    expect(updated.epochs).toEqual([1, 2, 3, 4, 5]);
    expect(updated.live_neuron_buffers.a.map((row) => row.epoch)).toEqual([4, 5]);
    expect(updated.live_neuron_buffers.b.at(-1)).toEqual({ epoch: 5, metrics: null });
    expect(metricValue({ epoch: 5, metrics: null }, "activation_mean")).toBeNull();
    expect(metricValue({ epoch: 5, metrics: { layer_id: "hidden", activation_mean: 0 } }, "activation_mean")).toBe(0);
  });

  it("replaces a duplicate epoch rather than duplicating chart points", () => {
    const updated = applySocketMessage(initial, {
      type: "epoch_update",
      epoch: 4,
      neurons: { a: { layer_id: "hidden", metrics: { layer_id: "hidden", activation_mean: 0.7 } } },
    });
    expect(updated.live_neuron_buffers.a).toHaveLength(1);
    expect(updated.live_neuron_buffers.a[0].metrics?.activation_mean).toBe(0.7);
  });

  it("updates recovery by its event ID without changing independent evidence", () => {
    const active = {
      ...initial,
      collapse_events: [{
        event_id: "evt-1",
        neuron_id: "a",
        layer_id: "hidden",
        event_type: "collapse",
        affected_classes: [0],
        detected_epoch: 3,
        evidence: { epochs: [1, 2, 3] },
        rule_version: "v1",
        status: "active",
        recovery: null,
      }],
    } satisfies LiveSnapshot;
    const recovered = applySocketMessage(active, {
      type: "collapse_recovered",
      event: { ...active.collapse_events[0], status: "recovered", recovered_epoch: 5 },
    });
    expect(recovered.collapse_events).toHaveLength(1);
    expect(recovered.collapse_events[0].status).toBe("recovered");
    expect(recovered.collapse_events[0].evidence).toEqual({ epochs: [1, 2, 3] });
  });

  it("uses backend neuron IDs and does not synthesize edges or extra IDs", () => {
    expect(visibleNeuronIds("hidden", 2, ["a", "b"], {})).toEqual(["a", "b"]);
    expect(visibleNeuronIds("hidden", 2, null, {})).toEqual(["hidden:0", "hidden:1"]);
  });
});
