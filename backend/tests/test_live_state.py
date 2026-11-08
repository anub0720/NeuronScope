import asyncio
import unittest

from fastapi import HTTPException

from app.auth import ProjectIdentity
from app.routes.metrics import get_live_snapshot, ingest_epoch
from app.schemas import EpochCreate
from app.state import LiveState, StateError, live_state


class LiveStateTests(unittest.TestCase):
    def setUp(self):
        self.state = LiveState()
        self.state.create_experiment(
            "exp-1", "project-1", "test", "pytorch", "MLP"
        )
        self.architecture = {
            "version": 1,
            "framework": "pytorch",
            "model_name": "MLP",
            "classes": {"0": "cat", "1": "dog"},
            "layers": [
                {
                    "layer_id": "layer-0",
                    "type": "Linear",
                    "neuron_count": 1,
                    "neuron_ids": ["neuron-0"],
                }
            ],
        }
        self.state.register_architecture(
            "exp-1", "project-1", self.architecture
        )

    def epoch(self, number, value):
        return {
            "epoch": number,
            "neurons": {
                "neuron-0": {
                    "layer_id": "layer-0",
                    "activation_mean": value,
                    "inactive_fraction": 0.1,
                    "class_contribution": [value, None],
                    "class_sample_counts": [10, None],
                    "class_validity_mask": [True, False],
                }
            },
        }

    def test_buffers_evict_oldest_and_keep_class_indices(self):
        for epoch in range(1, 7):
            self.state.ingest_epoch(
                "exp-1", "project-1", self.epoch(epoch, epoch / 10)
            )

        snapshot = self.state.snapshot("exp-1", "project-1")
        self.assertEqual(snapshot["epochs"], [2, 3, 4, 5, 6])
        values = snapshot["live_neuron_buffers"]["neuron-0"]
        self.assertEqual([row["epoch"] for row in values], [2, 3, 4, 5, 6])
        self.assertEqual(
            values[-1]["metrics"]["class_contribution"], [0.6, None]
        )
        self.assertEqual(
            snapshot["architecture"]["classes"], {"0": "cat", "1": "dog"}
        )

    def test_latest_duplicate_is_idempotent_and_conflicts_are_rejected(self):
        message = self.epoch(1, 0.1)
        first = self.state.ingest_epoch("exp-1", "project-1", message)
        retry = self.state.ingest_epoch("exp-1", "project-1", message)
        self.assertFalse(first["duplicate"])
        self.assertTrue(retry["duplicate"])
        with self.assertRaises(StateError):
            self.state.ingest_epoch("exp-1", "project-1", self.epoch(1, 0.2))

    def test_chunked_epoch_is_hidden_until_complete_and_replay_is_idempotent(self):
        state = LiveState()
        state.create_experiment("exp-chunk", "project-1", "test", "pytorch", "MLP")
        architecture = {
            "version": 1,
            "framework": "pytorch",
            "model_name": "MLP",
            "classes": {"0": "cat", "1": "dog"},
            "layers": [
                {
                    "layer_id": "layer-0",
                    "type": "Linear",
                    "neuron_count": 1,
                    "neuron_ids": ["neuron-0"],
                },
                {
                    "layer_id": "layer-1",
                    "type": "Linear",
                    "neuron_count": 1,
                    "neuron_ids": ["neuron-1"],
                },
            ],
        }
        state.register_architecture("exp-chunk", "project-1", architecture)
        first = {
            "neuron-0": {"layer_id": "layer-0", "activation_mean": 0.1}
        }
        second = {
            "neuron-1": {"layer_id": "layer-1", "activation_mean": 0.2}
        }

        self.assertFalse(
            state.stage_epoch_chunk(
                "exp-chunk", "project-1", 1, 0, 2, first
            )["duplicate"]
        )
        snapshot = state.snapshot("exp-chunk", "project-1")
        self.assertEqual(snapshot["current_epoch"], -1)
        self.assertEqual(snapshot["epochs"], [])
        self.assertEqual(snapshot["pending_epoch"]["received_chunks"], 1)
        with self.assertRaises(StateError):
            state.complete_staged_epoch("exp-chunk", "project-1", 1, 2)

        state.stage_epoch_chunk("exp-chunk", "project-1", 1, 1, 2, second)
        committed = state.complete_staged_epoch(
            "exp-chunk", "project-1", 1, 2
        )
        self.assertFalse(committed["duplicate"])
        self.assertTrue(
            state.complete_staged_epoch(
                "exp-chunk", "project-1", 1, 2
            )["duplicate"]
        )
        self.assertTrue(
            state.stage_epoch_chunk(
                "exp-chunk", "project-1", 1, 0, 2, first
            )["duplicate"]
        )
        self.assertEqual(
            state.snapshot("exp-chunk", "project-1")["epochs"], [1]
        )

    def test_chunked_epoch_rejects_conflicting_or_repeated_neurons(self):
        state = LiveState()
        state.create_experiment("exp-conflict", "project-1", "test", "pytorch", "MLP")
        state.register_architecture(
            "exp-conflict", "project-1", self.architecture
        )
        first = {"neuron-0": {"layer_id": "layer-0"}}
        state.stage_epoch_chunk(
            "exp-conflict", "project-1", 1, 0, 2, first
        )
        with self.assertRaises(StateError):
            state.stage_epoch_chunk(
                "exp-conflict", "project-1", 1, 0, 2,
                {"neuron-0": {"layer_id": "layer-0", "activation_mean": 1.0}},
            )
        with self.assertRaises(StateError):
            state.stage_epoch_chunk(
                "exp-conflict", "project-1", 1, 1, 2, first
            )

    def test_incomplete_chunked_epoch_cannot_be_finished(self):
        self.state.stage_epoch_chunk(
            "exp-1",
            "project-1",
            1,
            0,
            2,
            {"neuron-0": {"layer_id": "layer-0"}},
        )
        with self.assertRaises(StateError):
            self.state.finish_experiment("exp-1", "project-1")

    def test_bad_class_array_does_not_partially_register_neuron(self):
        state = LiveState()
        state.create_experiment("exp-3", "project-1", "test", "pytorch", "MLP")
        architecture = {**self.architecture}
        architecture["layers"] = [
            {
                "layer_id": "layer-0",
                "type": "Linear",
                "neuron_count": 2,
            }
        ]
        state.register_architecture("exp-3", "project-1", architecture)
        message = {
            "epoch": 1,
            "neurons": {
                "neuron-0": {"layer_id": "layer-0"},
                "neuron-1": {
                    "layer_id": "layer-0",
                    "class_contribution": [0.5],
                },
            },
        }
        with self.assertRaises(StateError):
            state.ingest_epoch("exp-3", "project-1", message)
        self.assertNotIn(
            "neuron-0",
            state.get("exp-3", "project-1").neuron_layers,
        )

    def test_event_snapshot_survives_live_buffer_eviction(self):
        for epoch in range(1, 6):
            self.state.ingest_epoch(
                "exp-1", "project-1", self.epoch(epoch, epoch / 10)
            )
        event = {
            "event_id": "event-1",
            "neuron_id": "neuron-0",
            "layer_id": "layer-0",
            "event_type": "contribution_collapse",
            "affected_classes": [1],
            "detected_epoch": 5,
            "evidence": None,
            "rule_version": "test-v1",
            "status": "active",
            "recovery": None,
        }
        self.state.register_event("exp-1", "project-1", event)
        before = self.state.get_event("exp-1", "project-1", "event-1")
        for epoch in range(6, 8):
            self.state.ingest_epoch(
                "exp-1", "project-1", self.epoch(epoch, epoch / 10)
            )
        after = self.state.get_event("exp-1", "project-1", "event-1")
        self.assertEqual(before["evidence"], after["evidence"])
        self.assertEqual(after["evidence"]["epochs"], [1, 2, 3, 4, 5])

    def test_missing_neuron_measurements_are_not_filled_with_zero(self):
        self.state.ingest_epoch("exp-1", "project-1", self.epoch(1, 0.1))
        self.state.ingest_epoch("exp-1", "project-1", {"epoch": 2, "neurons": {}})
        rows = self.state.snapshot("exp-1", "project-1")[
            "live_neuron_buffers"
        ]["neuron-0"]
        self.assertIsNone(rows[-1]["metrics"])

    def test_event_evidence_conflicting_with_live_data_is_rejected(self):
        self.state.ingest_epoch("exp-1", "project-1", self.epoch(1, 0.1))
        event = {
            "event_id": "event-conflict",
            "neuron_id": "neuron-0",
            "layer_id": "layer-0",
            "event_type": "contribution_collapse",
            "affected_classes": [1],
            "detected_epoch": 1,
            "evidence": {
                "epochs": [1],
                "activation_mean": [0.9],
            },
            "rule_version": "test-v1",
            "status": "active",
            "recovery": None,
        }
        with self.assertRaises(StateError):
            self.state.register_event("exp-1", "project-1", event)
        self.assertEqual(
            self.state.list_events("exp-1", "project-1"),
            [],
        )

    def test_events_deduplicate_recover_and_allow_reentry(self):
        base = {
            "event_id": "event-1",
            "neuron_id": "neuron-0",
            "layer_id": "layer-0",
            "event_type": "contribution_collapse",
            "affected_classes": [1],
            "detected_epoch": 3,
            "evidence": None,
            "rule_version": "test-v1",
            "status": "active",
            "recovery": None,
        }
        self.state.register_event("exp-1", "project-1", base)
        repeated = self.state.register_event("exp-1", "project-1", base)
        self.assertTrue(repeated["duplicate"])

        recovery = {
            **base,
            "event_id": "recovery-1",
            "detected_epoch": 4,
            "status": "recovered",
            "recovery": {"epoch": 4},
        }
        self.state.register_event("exp-1", "project-1", recovery)
        self.assertEqual(
            self.state.get_event("exp-1", "project-1", "event-1")["status"],
            "recovered",
        )
        self.assertTrue(
            self.state.register_event("exp-1", "project-1", recovery)["duplicate"]
        )

        reentry = {**base, "event_id": "event-2", "detected_epoch": 5}
        result = self.state.register_event("exp-1", "project-1", reentry)
        self.assertFalse(result["duplicate"])

    def test_clear_experiment_removes_state(self):
        self.state.ingest_epoch("exp-1", "project-1", self.epoch(1, 0.1))
        self.state.delete_experiment("exp-1", "project-1")
        with self.assertRaises(StateError):
            self.state.snapshot("exp-1", "project-1")


class LiveRouteTests(unittest.TestCase):
    def test_epoch_endpoint_uses_in_memory_state_and_checks_path_id(self):
        experiment_id = "route-test"
        project = ProjectIdentity("route-project", "route")
        live_state.create_experiment(
            experiment_id, project.id, "route test", "pytorch", "MLP"
        )
        live_state.register_architecture(
            experiment_id,
            project.id,
            {
                "version": 1,
                "framework": "pytorch",
                "model_name": "MLP",
                "classes": {"0": "cat"},
                "layers": [
                    {
                        "layer_id": "layer-0",
                        "type": "Linear",
                        "neuron_count": 1,
                        "neuron_ids": ["neuron-0"],
                    }
                ],
            },
        )
        try:
            response = ingest_epoch(
                experiment_id,
                EpochCreate(
                    experiment_id=experiment_id,
                    epoch=1,
                    neurons={
                        "neuron-0": {
                            "layer_id": "layer-0",
                            "activation_mean": 0.4,
                        }
                    },
                ),
                project,
            )
            self.assertEqual(response["status"], "accepted")
            snapshot = get_live_snapshot(experiment_id, project)
            self.assertEqual(snapshot["current_epoch"], 1)

            with self.assertRaises(HTTPException) as raised:
                ingest_epoch(
                    experiment_id,
                    EpochCreate(experiment_id="different", epoch=2, neurons={}),
                    project,
                )
            self.assertEqual(raised.exception.status_code, 422)
        finally:
            live_state.delete_experiment(experiment_id, project.id)


class WebSocketStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscriber_receives_incremental_epoch_update(self):
        manager = LiveState()
        manager.create_experiment("exp-2", "project-1", "test", "pytorch", "MLP")
        manager.register_architecture(
            "exp-2",
            "project-1",
            {
                "version": 1,
                "framework": "pytorch",
                "model_name": "MLP",
                "classes": {"0": "cat"},
                "layers": [
                    {
                        "layer_id": "layer-0",
                        "type": "Linear",
                        "neuron_count": 1,
                        "neuron_ids": ["neuron-0"],
                    }
                ],
            },
        )
        subscriber = manager.subscribe("exp-2", "project-1")
        manager.ingest_epoch(
            "exp-2",
            "project-1",
            {
                "epoch": 1,
                "neurons": {
                    "neuron-0": {
                        "layer_id": "layer-0",
                        "class_contribution": [0.4],
                    }
                },
            },
        )
        message = await asyncio.wait_for(subscriber.queue.get(), timeout=1)
        self.assertEqual(message["type"], "epoch_update")
        self.assertEqual(message["epoch"], 1)
        manager.unsubscribe("exp-2", subscriber.subscriber_id)

    async def test_backpressure_requests_snapshot_resynchronization(self):
        manager = LiveState()
        manager.create_experiment("exp-4", "project-1", "test", "pytorch", "MLP")
        manager.register_architecture(
            "exp-4",
            "project-1",
            {
                "version": 1,
                "framework": "pytorch",
                "model_name": "MLP",
                "classes": {"0": "cat"},
                "layers": [
                    {
                        "layer_id": "layer-0",
                        "type": "Linear",
                        "neuron_count": 1,
                        "neuron_ids": ["neuron-0"],
                    }
                ],
            },
        )
        subscriber = manager.subscribe("exp-4", "project-1")
        for epoch in range(1, 4):
            manager.ingest_epoch(
                "exp-4",
                "project-1",
                {
                    "epoch": epoch,
                    "neurons": {
                        "neuron-0": {
                            "layer_id": "layer-0",
                            "activation_mean": epoch / 10,
                        }
                    },
                },
            )
        await asyncio.sleep(0)
        message = await subscriber.queue.get()
        snapshot = manager.snapshot("exp-4", "project-1")
        self.assertEqual(message["type"], "resync_required")
        self.assertEqual(snapshot["current_epoch"], 3)
        self.assertEqual(snapshot["revision"], 3)
        manager.unsubscribe("exp-4", subscriber.subscriber_id)


if __name__ == "__main__":
    unittest.main()
