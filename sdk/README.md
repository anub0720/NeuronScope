# NeuronScope Python SDK

The NeuronScope SDK streams bounded, per-neuron training metrics from supported
PyTorch layers to a NeuronScope backend.

## Install

From the repository root:

```powershell
python -m pip install -e .\sdk
```

In Kaggle, install from a published package or a repository checkout. PyTorch
is a runtime dependency; use the version appropriate for your Kaggle runtime.
Kaggle does not provide the desktop OS credential manager and cannot complete
the interactive device flow. Create a project and issue a named SDK credential
in the dashboard, then store the endpoint, project ID, and key in Kaggle
Secrets.

## Quick start

Set `NEURONSCOPE_ENDPOINT` to the HTTPS backend origin, then initialize with
the model and experiment metadata:

```python
import neuronscope

monitor = neuronscope.init(
    model=model,
    experiment_name="experiment_001",
    class_mapping={0: "cat", 1: "dog"},
)

try:
    for epoch in range(epochs):
        train_one_epoch(model, train_loader)
        monitor.log_epoch(epoch=epoch, probe_loader=probe_loader)
finally:
    monitor.finish()
```

On a trusted workstation with a secure OS credential store, the SDK starts a
short-lived device authorization, opens the dashboard approval page, and waits
for a verified account holder to approve it. The SDK retains the returned
project ID and API key in the operating system credential manager; subsequent
runs reuse that project. Pass `project_id` to reuse a particular owned
project. Credentials supplied explicitly or through `NEURONSCOPE_PROJECT_ID`
and `NEURONSCOPE_API_KEY` bypass device authorization and are validated against
the backend before initialization.

Initialization queues experiment and architecture registration without waiting
for dashboard rendering or network completion. `log_epoch` collects available
metrics synchronously, updates its bounded history, and enqueues one compact
epoch message. Transport retries happen on a background worker.

## Kaggle example

Set the endpoint and project credentials as Kaggle Secrets or environment
variables rather than writing them into a notebook:

```python
import os
import neuronscope

monitor = neuronscope.init(
    model=model,
    project_id=os.environ["NEURONSCOPE_PROJECT_ID"],
    experiment_name="kaggle-run",
    endpoint=os.environ["NEURONSCOPE_ENDPOINT"],
    api_key=os.environ["NEURONSCOPE_API_KEY"],
    class_mapping=class_names,
)

try:
    for epoch in range(num_epochs):
        train_one_epoch(model, train_loader)
        monitor.log_epoch(epoch, probe_loader=probe_loader)
finally:
    monitor.finish()
```

For a complete Kaggle notebook using Kaggle Secrets and a small training run,
see [the runnable quickstart](examples/kaggle_quickstart.ipynb).

If a Kaggle process itself restarts, its in-memory retry queue, metrics, and
event snapshots are lost unless the caller deliberately saves its own
experiment ID and reconstructs training state. The SDK creates no local
metric files or checkpoints.

## Supported modules and limitations

Architecture discovery supports `torch.nn.Linear`, `Conv1d`, `Conv2d`, and
`Conv3d`. IDs are stable for a given named module and output index. Branches
are identified by module name; the SDK does not infer connections from module
order. Residual graph edges, reused/shared-module execution semantics, dynamic
module creation, and distributed metric aggregation are not represented.
Use one monitor on the process that owns the model; do not have multiple
distributed ranks submit the same experiment epoch.

The SDK does not move the model, change optimizer state, or call `train()` on
the user's behalf. Forward hooks do not replace outputs. Gradient summaries
are collected only where autograd computes gradients for the hooked outputs.
Metrics whose output tensors were not observed remain unavailable rather than
being reported as zero.

## Metrics

- Activation mean and population variance summarize all observed values per
  neuron across the epoch. Minimum/maximum and selected quantiles are collected
  when enabled. `inactive_fraction` is the fraction whose absolute value is at
  or below the configured `inactive_epsilon` (default: exact zero).
- `gradient_mean` and `gradient_variance` summarize absolute output gradients.
  The backend currently has no gradient-norm or zero-gradient-fraction fields.
- `weight_norm` is the L2 norm of each output neuron's/channel's weight slice.
  Weight mean/variance and bias metrics are not currently accepted by the
  backend schema.
- Class contribution is opt-in. For class `c`, it is the average per-example
  cross-entropy loss after zeroing a selected neuron, minus the baseline loss
  for the same examples of class `c`. Positive values indicate that ablation
  worsened the loss. It is a diagnostic approximation for classification
  models, not a causal claim about arbitrary models.

## Performance and memory

The SDK retains a configurable ring buffer (five epochs by default), a bounded
transport queue, and bounded event snapshots in RAM only. Activation and
gradient observations are reduced immediately; full tensors and computation
graphs are not retained. The transport queue coalesces old epoch messages in
favor of a resynchronization from the latest rolling window. If it contains only
events and reaches capacity, event delivery can wait while the detector retains
the evidence in its bounded in-memory history. `status()` exposes pending-event
counts, delivery failures, retention errors, and dropped-epoch counters.
`finish()` reports failure if events have not been acknowledged before its
timeout; `clear()` explicitly discards pending evidence. Delivery cannot be
guaranteed if the process exits or the backend remains unavailable beyond the
configured in-memory retention limits.

The default event-evidence window is five epochs, matching the backend's
default `METRIC_WINDOW_SIZE`. If the backend uses another value, set
`event_evidence_window_size` to that same value. The SDK keeps this separate
from its local `window_size` so restart recovery can retain a larger or smaller
metric history without sending evidence the backend rejects.

Class-wise ablation is disabled by default. It runs extra evaluation forwards:
roughly one baseline plus one forward per selected neuron per probe batch. Set
`collect_contributions=True`, `max_contribution_neurons`, and
`contribution_probe_batches` deliberately for small diagnostic subsets.
`probe_loader` must yield `(inputs, integer_labels)` and model output must be a
`[batch, classes]` logits tensor. The SDK temporarily evaluates the model,
then restores every module's prior training flag and CPU/CUDA RNG state.
BatchNorm evaluation does not update running statistics. Custom model forward
side effects beyond these PyTorch state mechanisms cannot be guaranteed to be
reversible.

## Collapse rules

No collapse rule is enabled by default. Rules require user-provided thresholds
and consecutive epochs. Activation magnitude or inactivity alone is not
treated as collapse. For example:

```python
from neuronscope import CollapseRule, MonitorConfig

config = MonitorConfig(
    collect_contributions=True,
    max_contribution_neurons=8,
    collapse_rules=(
        CollapseRule(
            rule_id="class-contribution-drop",
            metric="class_contribution",
            operator="below",
            threshold=-0.2,
            consecutive_epochs=2,
            affected_classes=(0,),
            recovery_threshold=0.0,
        ),
    ),
)
```

Events include an independent copy of the recent metric evidence and are
deduplicated until recovery. Recovery is sent using the backend's existing
`status: "recovered"` event request.

## Lifecycle and troubleshooting

- `monitor.log_epoch(epoch, probe_loader=None)` requires strictly increasing,
  non-negative integer epochs.
- `monitor.status()` reports connection state, queued messages, errors, and
  dropped counts, pending events, event delivery failures, and event retention
  errors.
- `monitor.finish()` removes hooks, attempts to flush queued messages for a
  bounded time, then calls the backend finish endpoint.
- `monitor.clear()` removes hooks and SDK buffers and requests backend
  in-memory cleanup. It does not delete database metadata.
- A `401` indicates an invalid project API key. A `409` architecture error
  means the server already has a different architecture for the experiment.
  A `413` indicates the configured backend request limit was exceeded.
- Restart recovery reuses the explicit experiment ID, re-registers architecture,
  and resends the SDK's recent epochs/events. The backend must be configured
  with one worker because its live state is process-local.

See [the backend compatibility contract](../docs/sdk-backend-compatibility.md)
for exact HTTP/WebSocket routes, payloads, and unsupported backend fields.

## Tests

```powershell
python -m pip install -e ".[test]"
python -m pytest
```
