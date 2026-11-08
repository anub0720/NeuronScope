import json
import threading
import time

import httpx
import pytest
import torch
from torch import nn

from neuronscope.architecture import discover_architecture, normalize_class_mapping
from neuronscope.buffers import MetricBuffer
from neuronscope.client import Monitor
from neuronscope.collapse import CollapseDetector
from neuronscope.collectors.activation import RunningNeuronStats
from neuronscope.collectors.contribution import ContributionCollector
from neuronscope.collectors.weights import collect_weight_metrics
from neuronscope.config import CollapseRule, MonitorConfig
from neuronscope.exceptions import ConfigurationError
from neuronscope.serialization import omit_missing
from neuronscope.transport import AsyncTransport


def _transport(
    config,
    handler,
    state_provider=None,
    event_provider=None,
    event_acknowledger=None,
):
    state_provider = state_provider or (lambda: ([], []))
    return AsyncTransport(
        endpoint="https://example.test",
        api_key="test-key",
        experiment_id="experiment-1",
        experiment_name="test",
        model_name="Model",
        architecture={
            "experiment_id": "experiment-1",
            "version": 1,
            "framework": "pytorch",
            "model_name": "Model",
            "classes": {"0": "zero", "1": "one"},
            "layers": [
                {
                    "layer_id": "linear",
                    "type": "Linear",
                    "neuron_count": 2,
                    "neuron_ids": ["n-0", "n-1"],
                }
            ],
        },
        config=config,
        state_provider=state_provider,
        event_provider=event_provider,
        event_acknowledger=event_acknowledger,
        http_transport=httpx.MockTransport(handler),
    )


def _response(request, status=200):
    return httpx.Response(status, json={"status": "accepted"}, request=request)


def _wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_architecture_discovery_and_class_mapping():
    model = nn.Sequential(nn.Conv2d(1, 3, 3), nn.Flatten(), nn.Linear(12, 2))
    architecture = discover_architecture(model)

    assert [layer.layer_id for layer in architecture.layers] == ["0", "2"]
    assert [layer.neuron_count for layer in architecture.layers] == [3, 2]
    assert len(set(architecture.neuron_to_layer)) == 5
    assert normalize_class_mapping({1: "dog", 0: "cat"}) == {
        "0": "cat",
        "1": "dog",
    }


def test_class_mapping_rejects_noncanonical_keys_and_labels():
    with pytest.raises(ValueError, match="canonical"):
        normalize_class_mapping({"00": "cat"})
    with pytest.raises(ValueError, match="non-empty strings"):
        normalize_class_mapping({0: ""})


def test_running_statistics_accumulate_without_retaining_tensors():
    stats = RunningNeuronStats(2, quantiles=(0.5,), quantile_samples_per_neuron=8)
    stats.add(torch.tensor([[1.0, 2.0], [3.0, 4.0]]), feature_axis=1)
    stats.add(torch.tensor([[5.0, 6.0]]), feature_axis=1)

    rows = stats.metrics()
    assert rows[0]["sample_count"] == 3
    assert rows[0]["mean"] == pytest.approx(3.0)
    assert rows[0]["variance"] == pytest.approx(8.0 / 3.0)
    assert rows[0]["quantiles"]["0.5"] in (1.0, 3.0, 5.0)
    assert rows[1]["mean"] == pytest.approx(4.0)


def test_metric_buffer_is_bounded_and_copies_input():
    buffer = MetricBuffer(2, {"n-0": "linear"}, class_count=2)
    metrics = {"n-0": {"class_contribution": [0.1, None]}}
    buffer.append(0, metrics)
    metrics["n-0"]["class_contribution"][0] = 9.0
    buffer.append(1, {"n-0": {}})
    buffer.append(2, {"n-0": {}})

    assert buffer.epochs == [1, 2]
    assert buffer.latest("n-0")[0]["metrics"]["layer_id"] == "linear"
    with pytest.raises(ValueError, match="increase monotonically"):
        buffer.append(2, {"n-0": {}})


def test_metric_buffer_retains_only_the_latest_five_epochs():
    buffer = MetricBuffer(5, {"n-0": "linear"}, class_count=2)
    for epoch in range(8):
        buffer.append(epoch, {"n-0": {"activation_mean": float(epoch)}})

    assert buffer.epochs == [3, 4, 5, 6, 7]
    assert [row["epoch"] for row in buffer.latest("n-0")] == [3, 4, 5, 6, 7]


def test_weight_collector_emits_only_backend_supported_fields():
    model = nn.Linear(2, 2, bias=True)
    layer = discover_architecture(model).layers[0]

    metrics = collect_weight_metrics((layer,))

    assert set(metrics) == set(layer.neuron_ids)
    assert all(set(row) == {"weight_norm"} for row in metrics.values())


def test_serialization_preserves_integer_and_boolean_values():
    assert omit_missing(
        {"counts": [2, None], "validity": [True, False], "value": float("nan")}
    ) == {"counts": [2, None], "validity": [True, False], "value": None}


def test_collapse_detector_triggers_recovers_and_bounds_evidence():
    rule = CollapseRule(
        rule_id="variance-low",
        metric="activation_variance",
        operator="below",
        threshold=0.1,
        consecutive_epochs=2,
        recovery_threshold=0.2,
    )
    detector = CollapseDetector((rule,), class_count=2, max_event_history=4)
    history = [
        {"epoch": 0, "metrics": {"layer_id": "linear", "activation_variance": 0.05}},
        {"epoch": 1, "metrics": {"layer_id": "linear", "activation_variance": 0.06}},
    ]
    active = detector.process(
        "experiment-1",
        1,
        {"n-0": history[-1]["metrics"]},
        {"n-0": history},
    )

    assert len(active) == 1
    assert active[0]["evidence"]["epochs"] == [0, 1]
    recovered = detector.process(
        "experiment-1",
        2,
        {"n-0": {"layer_id": "linear", "activation_variance": 0.25}},
        {"n-0": history + [
            {"epoch": 2, "metrics": {"layer_id": "linear", "activation_variance": 0.25}}
        ]},
    )
    assert recovered[0]["status"] == "recovered"
    assert detector.events[0]["status"] == "recovered"

    limited = CollapseDetector((), class_count=2, max_event_history=2, evidence_window_size=2)
    long_history = [
        {"epoch": epoch, "metrics": {"layer_id": "linear"}}
        for epoch in range(5)
    ]
    assert limited.history_snapshot(long_history)["epochs"] == [3, 4]


def test_contribution_collector_restores_training_mode_and_rng():
    model = nn.Sequential(nn.Linear(2, 2, bias=False))
    with torch.no_grad():
        model[0].weight.copy_(torch.eye(2))
    model.train()
    layer = discover_architecture(model).layers[0]
    config = MonitorConfig(
        collect_contributions=True,
        contribution_neurons=(("0", 0),),
    )
    rng_before = torch.random.get_rng_state().clone()
    collector = ContributionCollector(
        model,
        (layer,),
        class_count=2,
        config=config,
    )

    result = collector.collect(
        [(torch.tensor([[2.0, 0.0], [0.0, 2.0]]), torch.tensor([0, 1]))]
    )

    assert model.training
    assert torch.equal(torch.random.get_rng_state(), rng_before)
    contribution = result[layer.neuron_ids[0]]["class_contribution"]
    assert contribution[0] is not None and contribution[0] > 0
    assert result[layer.neuron_ids[0]]["class_sample_counts"] == [1, 1]
    assert result[layer.neuron_ids[0]]["class_validity_mask"] == [True, True]
    assert result[layer.neuron_ids[0]][
        "class_contribution_uncertainty"
    ] == [None, None]


def test_monitor_posts_backend_compatible_epoch_and_auth_header():
    requests = []

    async def handler(request):
        requests.append(request)
        return _response(request)

    model = nn.Sequential(nn.Linear(2, 2))
    monitor = Monitor(
        model,
        project_id="project-1",
        experiment_name="test",
        endpoint="https://example.test",
        api_key="test-key",
        class_mapping={0: "zero", 1: "one"},
        _http_transport=httpx.MockTransport(handler),
    )
    values = model(torch.tensor([[1.0, 2.0], [3.0, 4.0]]))
    values.sum().backward()
    monitor.log_epoch(0)

    assert monitor.finish(timeout=3)
    epoch_request = next(request for request in requests if request.url.path.endswith("/epochs"))
    payload = json.loads(epoch_request.content)
    allowed = {
        "layer_id",
        "activation_mean",
        "activation_variance",
        "activation_quantiles",
        "inactive_fraction",
        "gradient_mean",
        "gradient_variance",
        "weight_norm",
        "class_contribution",
        "class_sample_counts",
        "class_validity_mask",
        "class_contribution_uncertainty",
        "sample_count",
        "uncertainty",
    }
    assert all(set(metrics) <= allowed for metrics in payload["neurons"].values())
    assert epoch_request.headers["Authorization"] == "Bearer test-key"
    assert all(request.headers["Authorization"] == "Bearer test-key" for request in requests)


def test_transport_retries_transient_errors():
    attempts = 0
    connected = threading.Event()

    async def handler(request):
        nonlocal attempts
        if request.url.path.endswith("/experiments") and request.method == "POST":
            attempts += 1
            if attempts == 1:
                return _response(request, status=503)
        if request.url.path.endswith("/architecture"):
            connected.set()
        return _response(request)

    transport = _transport(MonitorConfig(), handler)
    transport.start()
    assert transport.flush(timeout=4)
    assert connected.wait(timeout=1)
    assert attempts == 2
    assert transport.status()["connected"]
    transport.shutdown()


def test_transport_recovers_after_live_state_disappears():
    requests = []
    epoch_attempts = 0
    epoch_payload = {
        "experiment_id": "experiment-1",
        "epoch": 0,
        "neurons": {"n-0": {"layer_id": "linear", "weight_norm": 1.0}},
    }

    async def handler(request):
        nonlocal epoch_attempts
        requests.append(request)
        if request.url.path.endswith("/epochs"):
            epoch_attempts += 1
            if epoch_attempts == 1:
                return _response(request, status=404)
        return _response(request)

    transport = _transport(
        MonitorConfig(),
        handler,
        state_provider=lambda: ([epoch_payload], []),
    )
    transport.start()
    transport.submit_epoch(epoch_payload)

    assert transport.flush(timeout=3)
    assert epoch_attempts == 2
    assert sum(request.url.path.endswith("/architecture") for request in requests) == 2
    transport.shutdown()


def test_registration_and_epoch_order_precedes_event_delivery():
    requests = []
    pending = [{
        "experiment_id": "experiment-1",
        "event_id": "event-1",
        "neuron_id": "n-0",
        "layer_id": "linear",
        "event_type": "test-collapse",
        "affected_classes": [],
        "detected_epoch": 0,
        "evidence": None,
        "rule_version": "test",
        "status": "active",
        "recovery": None,
    }]
    epoch = {
        "experiment_id": "experiment-1",
        "epoch": 0,
        "neurons": {"n-0": {"layer_id": "linear"}},
    }

    async def handler(request):
        requests.append(request)
        return _response(request)

    transport = _transport(
        MonitorConfig(),
        handler,
        state_provider=lambda: ([epoch], pending),
        event_provider=lambda: list(pending),
        event_acknowledger=lambda event_id: pending.clear(),
    )
    transport.start()
    transport.submit_epoch(epoch)
    transport.submit_event(pending[0])

    assert transport.flush(timeout=3)
    operations = [request.url.path.rsplit("/", 1)[-1] for request in requests]
    assert operations[:4] == ["experiments", "architecture", "epochs", "events"]
    assert pending == []
    transport.shutdown()


def test_event_failure_remains_pending_until_acknowledged():
    requests = []
    pending = [{
        "experiment_id": "experiment-1",
        "event_id": "event-pending",
        "neuron_id": "n-0",
        "layer_id": "linear",
        "event_type": "test-collapse",
        "affected_classes": [],
        "detected_epoch": 0,
        "evidence": None,
        "rule_version": "test",
        "status": "active",
        "recovery": None,
    }]
    fail_event = True

    async def handler(request):
        nonlocal fail_event
        requests.append(request)
        if request.url.path.endswith("/events") and fail_event:
            return _response(request, status=401)
        return _response(request)

    transport = _transport(
        MonitorConfig(retry_max_delay=0.1),
        handler,
        event_provider=lambda: list(pending),
        event_acknowledger=lambda event_id: pending.clear(),
    )
    transport.start()
    transport.submit_event(pending[0])
    assert _wait_until(lambda: transport.status()["event_delivery_failures"] == 1)
    assert transport.status()["pending_events"] == 1
    assert pending[0]["event_id"] == "event-pending"
    assert transport.status()["last_event_error"]

    fail_event = False
    assert transport.flush(timeout=3)
    assert pending == []
    assert sum(request.url.path.endswith("/events") for request in requests) >= 2
    transport.shutdown()


def test_reserved_event_capacity_drains_without_losing_backlogged_events():
    requests = []
    pending = [
        {
            "experiment_id": "experiment-1",
            "event_id": f"event-{index}",
            "neuron_id": "n-0",
            "layer_id": "linear",
            "event_type": "test-collapse",
            "affected_classes": [],
            "detected_epoch": index,
            "evidence": None,
            "rule_version": "test",
            "status": "active",
            "recovery": None,
        }
        for index in range(2)
    ]

    async def handler(request):
        requests.append(request)
        return _response(request)

    def acknowledge(event_id):
        pending[:] = [event for event in pending if event["event_id"] != event_id]

    transport = _transport(
        MonitorConfig(
            event_queue_size=1,
            event_queue_bytes=1024,
            max_request_bytes=1024,
        ),
        handler,
        event_provider=lambda: list(pending),
        event_acknowledger=acknowledge,
    )
    transport.start()
    transport.submit_event(pending[0])

    assert transport.flush(timeout=3)
    assert pending == []
    assert [
        json.loads(request.content)["event_id"]
        for request in requests
        if request.url.path.endswith("/events")
    ] == ["event-0", "event-1"]
    transport.shutdown()


def test_large_epoch_is_layer_chunked_and_completed_atomically():
    requests = []
    neurons = {
        f"n-{index}": {
            "layer_id": f"layer-{index // 6}",
            "activation_mean": float(index),
            "activation_quantiles": {str(q): float(index) for q in range(20)},
        }
        for index in range(12)
    }
    epoch = {
        "experiment_id": "experiment-1",
        "epoch": 7,
        "neurons": neurons,
    }

    async def handler(request):
        requests.append(request)
        return _response(request)

    transport = _transport(
        MonitorConfig(max_request_bytes=1024),
        handler,
        state_provider=lambda: ([epoch], []),
    )
    transport.start()
    transport.submit_epoch(epoch)

    assert transport.flush(timeout=3)
    chunk_requests = [
        request for request in requests if request.url.path.endswith("/chunks")
    ]
    complete_requests = [
        request for request in requests if request.url.path.endswith("/complete")
    ]
    assert len(chunk_requests) > 2
    assert len(complete_requests) == 1
    assert all(len(request.content) <= 1024 for request in chunk_requests)
    assert all(
        len({metrics["layer_id"] for metrics in json.loads(request.content)["neurons"].values()}) == 1
        for request in chunk_requests
    )
    assert requests.index(chunk_requests[-1]) < requests.index(complete_requests[0])
    transport.shutdown()


def test_transport_queue_drops_oldest_epoch_when_full():
    config = MonitorConfig(retry_queue_size=3)
    transport = _transport(
        config,
        lambda request: _response(request),
    )

    for epoch in range(4):
        assert transport.enqueue("epoch", {"epoch": epoch})

    assert [job.payload["epoch"] for job in transport._queue] == [1, 2, 3]
    assert transport.status()["dropped_epochs"] == 1


def test_transport_finish_reports_permanent_delivery_failure():
    async def handler(request):
        if request.url.path.endswith("/finish"):
            return _response(request, status=401)
        return _response(request)

    transport = _transport(MonitorConfig(), handler)
    transport.start()

    assert not transport.finish(timeout=3)
    assert transport.status()["pending_messages"] == 1
    assert "finish delivery failed" in transport.status()["last_error"]
    transport.shutdown()


def test_clear_removes_hooks_and_requests_backend_cleanup():
    requests = []

    async def handler(request):
        requests.append(request)
        return _response(request)

    model = nn.Sequential(nn.Linear(2, 2))
    monitor = Monitor(
        model,
        project_id="project-1",
        experiment_name="test",
        endpoint="https://example.test",
        api_key="test-key",
        class_mapping={0: "zero", 1: "one"},
        _http_transport=httpx.MockTransport(handler),
    )

    assert model[0]._forward_hooks
    assert monitor.clear(timeout=3)
    assert not model[0]._forward_hooks
    assert any(request.method == "DELETE" for request in requests)


def test_config_rejects_nonfinite_timeout():
    with pytest.raises(ConfigurationError, match="finite"):
        MonitorConfig(request_timeout=float("inf"))
