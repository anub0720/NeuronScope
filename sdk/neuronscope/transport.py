"""Bounded asynchronous HTTPX transport with retries and restart recovery."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from neuronscope.config import MonitorConfig
from neuronscope.exceptions import PayloadTooLargeError, TransportError

logger = logging.getLogger("neuronscope")


@dataclass(frozen=True)
class _Job:
    kind: str
    payload: dict[str, Any]
    size: int
    completion: "_Completion | None" = None


@dataclass
class _Completion:
    event: threading.Event = field(default_factory=threading.Event)
    succeeded: bool = False


class AsyncTransport:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        experiment_id: str,
        experiment_name: str,
        model_name: str,
        architecture: dict[str, Any],
        config: MonitorConfig,
        state_provider: Callable[[], tuple[list[dict[str, Any]], list[dict[str, Any]]]],
        epoch_provider: Callable[[int], dict[str, Any] | None] | None = None,
        event_provider: Callable[[], list[dict[str, Any]]] | None = None,
        event_acknowledger: Callable[[str], None] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.experiment_id = experiment_id
        self.experiment_name = experiment_name
        self.model_name = model_name
        self.architecture = copy.deepcopy(architecture)
        self.config = config
        self._state_provider = state_provider
        self._epoch_provider = epoch_provider or self._find_epoch_in_state
        self._event_provider = event_provider or (lambda: [])
        self._event_acknowledger = event_acknowledger or (lambda _event_id: None)
        self._http_transport = http_transport
        self._queue: deque[_Job] = deque()
        self._queue_bytes = 0
        self._pending_events: dict[str, _Job] = {}
        self._pending_event_bytes = 0
        self._event_retry_at: dict[str, float] = {}
        self._failed_event_ids: set[str] = set()
        self._event_delivery_failures = 0
        self._last_event_error: str | None = None
        self._queue_condition = threading.Condition()
        self._state_lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._in_flight = False
        self._connected = False
        self._last_error: str | None = None
        self._dropped_epochs = 0
        self._dropped_events = 0
        self._dropped_messages = 0
        self._in_flight_kind: str | None = None
        self._clearing = False
        self._queue_retry_at = 0.0
        self._registration_stage = "pending_experiment"

    def start(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._thread_main,
            name=f"neuronscope-{self.experiment_id[:8]}",
            daemon=True,
        )
        self._worker.start()
        if not self._ready.wait(timeout=5):
            raise TransportError("Timed out starting the HTTP transport worker")
        if not self.enqueue(
            "experiment",
            {
                "experiment_id": self.experiment_id,
                "name": self.experiment_name,
                "framework": "pytorch",
                "model_name": self.model_name,
            },
        ):
            self.shutdown()
            raise TransportError("Could not queue experiment registration")
        if not self.enqueue("architecture", self.architecture):
            self.shutdown()
            raise TransportError("Could not queue architecture registration")

    def submit_epoch(self, payload: dict[str, Any]) -> None:
        self.enqueue("epoch", {"epoch": payload["epoch"]})

    def submit_event(self, _payload: dict[str, Any]) -> None:
        with self._queue_condition:
            self._queue_condition.notify_all()

    def finish(self, timeout: float) -> bool:
        if not self.flush(timeout):
            return False
        completion = _Completion()
        queued = self.enqueue(
            "finish",
            {"experiment_id": self.experiment_id},
            _completion=completion,
        )
        flushed = self.flush(timeout) if queued else False
        succeeded = flushed and completion.event.is_set() and completion.succeeded
        if succeeded:
            self.shutdown(timeout=max(0.1, min(timeout, self.config.request_timeout)))
        return succeeded

    def clear(self, timeout: float) -> bool:
        with self._queue_condition:
            self._clearing = True
            self._queue.clear()
            self._queue_bytes = 0
            self._pending_events.clear()
            self._pending_event_bytes = 0
            self._event_retry_at.clear()
            self._queue_condition.notify_all()
        completion = _Completion()
        queued = self.enqueue(
            "clear",
            {"experiment_id": self.experiment_id},
            _completion=completion,
        )
        flushed = self.flush(timeout) if queued else False
        succeeded = flushed and completion.event.is_set() and completion.succeeded
        self.shutdown(timeout=max(0.1, min(timeout, self.config.request_timeout)))
        return succeeded

    def flush(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self._refresh_events()
            with self._queue_condition:
                pending = bool(
                    self._queue or self._pending_events or self._in_flight
                )
                if not pending:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Transport flush timed out with %d queued messages and %d pending events",
                        len(self._queue),
                        len(self._pending_events),
                    )
                    return False
                self._queue_condition.wait(min(remaining, 0.1))

    def shutdown(self, timeout: float = 1.0) -> None:
        self._stop.set()
        with self._queue_condition:
            self._queue_condition.notify_all()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(timeout, 0.1))
            if worker.is_alive():
                logger.warning("Transport worker did not stop before its timeout")

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        _completion: _Completion | None = None,
    ) -> bool:
        if kind == "event":
            self.submit_event(payload)
            return payload.get("event_id") in self._pending_events
        try:
            encoded = json.dumps(
                payload, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            self._record_error(f"Cannot serialize {kind} payload: {exc}")
            self._count_drop(kind)
            return False
        size = len(encoded)
        if size > self.config.max_request_bytes:
            error = PayloadTooLargeError(
                f"{kind} payload is {size} bytes; limit is "
                f"{self.config.max_request_bytes}"
            )
            self._record_error(str(error))
            self._count_drop(kind)
            logger.error("%s", error)
            return False

        with self._queue_condition:
            if self._stop.is_set():
                self._record_error("Transport is stopped")
                self._count_drop(kind)
                return False
            while (
                len(self._queue) >= self.config.retry_queue_size
                or self._queue_bytes + size > self.config.retry_queue_bytes
            ):
                if not self._discard_oldest_epoch_locked():
                    self._count_drop(kind)
                    self._record_error(
                        f"Retry queue full; dropped {kind} message"
                    )
                    logger.warning("Retry queue full; dropped %s message", kind)
                    return False
            self._queue.append(
                _Job(kind, copy.deepcopy(payload), size, _completion)
            )
            self._queue_bytes += size
            self._queue_condition.notify_all()
            return True

    def _discard_oldest_epoch_locked(self) -> bool:
        for index, job in enumerate(self._queue):
            if job.kind == "epoch":
                del self._queue[index]
                self._queue_bytes -= job.size
                self._dropped_epochs += 1
                self._dropped_messages += 1
                logger.warning(
                    "Dropped queued epoch %s to preserve recent recovery state",
                    job.payload.get("epoch"),
                )
                return True
        return False

    def _refresh_events(self) -> None:
        if self._clearing:
            return
        try:
            messages = self._event_provider()
        except Exception as exc:
            self._record_event_error(f"Could not read pending events: {exc}")
            return
        with self._queue_condition:
            self._refresh_events_locked(messages)

    def _find_epoch_in_state(self, epoch: int) -> dict[str, Any] | None:
        epochs, _events = self._state_provider()
        return next((payload for payload in epochs if payload.get("epoch") == epoch), None)

    def _refresh_events_locked(
        self, messages: list[dict[str, Any]]
    ) -> None:
        for payload in messages:
            event_id = payload.get("event_id")
            if not isinstance(event_id, str) or event_id in self._pending_events:
                continue
            try:
                encoded = json.dumps(
                    payload, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
            except (TypeError, ValueError) as exc:
                self._record_event_error(
                    f"Cannot serialize collapse event: {exc}"
                )
                continue
            size = len(encoded)
            if size > self.config.max_request_bytes:
                self._record_event_error(
                    f"Collapse event {event_id} exceeds max_request_bytes"
                )
                continue
            if (
                len(self._pending_events) >= self.config.event_queue_size
                or self._pending_event_bytes + size > self.config.event_queue_bytes
            ):
                self._record_event_error(
                    "Reserved collapse-event delivery capacity is full; "
                    "the event remains in the monitor's bounded RAM history"
                )
                continue
            self._pending_events[event_id] = _Job("event", copy.deepcopy(payload), size)
            self._pending_event_bytes += size
            self._failed_event_ids.discard(event_id)
        self._queue_condition.notify_all()

    def status(self) -> dict[str, Any]:
        self._refresh_events()
        with self._queue_condition, self._state_lock:
            return {
                "connected": self._connected,
                "pending_messages": (
                    len(self._queue)
                    + len(self._pending_events)
                    + int(self._in_flight and self._in_flight_kind != "event")
                ),
                "pending_bytes": self._queue_bytes,
                "pending_events": len(self._pending_events),
                "event_delivery_failures": self._event_delivery_failures,
                "last_event_error": self._last_event_error,
                "registration_stage": self._registration_stage,
                "dropped_epochs": self._dropped_epochs,
                "dropped_events": self._dropped_events,
                "dropped_messages": self._dropped_messages,
                "last_error": self._last_error,
            }

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as exc:
            self._record_error(f"Transport worker failed: {exc}")
            logger.exception("Transport worker stopped unexpectedly")
        finally:
            self._ready.set()
            with self._queue_condition:
                self._connected = False
                self._queue_condition.notify_all()

    async def _serve(self) -> None:
        timeout = httpx.Timeout(self.config.request_timeout)
        async with httpx.AsyncClient(
            timeout=timeout,
            transport=self._http_transport,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as client:
            self._ready.set()
            while not self._stop.is_set():
                job = await asyncio.to_thread(self._take_next)
                if job is None:
                    break
                succeeded = False
                try:
                    succeeded = await self._deliver(client, job)
                finally:
                    if succeeded:
                        if job.kind == "experiment":
                            self._registration_stage = "experiment_registered"
                        elif job.kind == "architecture":
                            self._registration_stage = "ready"
                        elif job.kind == "event":
                            try:
                                self._event_acknowledger(job.payload["event_id"])
                            except Exception as exc:
                                succeeded = False
                                self._record_event_error(
                                    f"Backend acknowledged event but local acknowledgement failed: {exc}"
                                )
                            else:
                                with self._queue_condition:
                                    event_id = job.payload["event_id"]
                                    retained = self._pending_events.pop(event_id, None)
                                    if retained is not None:
                                        self._pending_event_bytes -= retained.size
                                    self._event_retry_at.pop(event_id, None)
                                    self._failed_event_ids.discard(event_id)
                                    self._last_event_error = None
                                    self._queue_condition.notify_all()
                    elif job.kind == "event":
                        event_id = job.payload["event_id"]
                        self._event_retry_at[event_id] = (
                            time.monotonic() + min(5.0, self.config.retry_max_delay)
                        )
                    else:
                        with self._queue_condition:
                            self._queue.appendleft(job)
                            self._queue_bytes += job.size
                            self._queue_retry_at = (
                                time.monotonic()
                                + min(5.0, self.config.retry_max_delay)
                            )
                    if job.completion is not None:
                        job.completion.succeeded = succeeded
                        job.completion.event.set()
                    with self._queue_condition:
                        self._in_flight = False
                        self._in_flight_kind = None
                        self._queue_condition.notify_all()

    def _take_next(self) -> _Job | None:
        while True:
            self._refresh_events()
            with self._queue_condition:
                if self._stop.is_set():
                    return None
                now = time.monotonic()
                if self._queue:
                    job = self._queue[0]
                    if job.kind not in {"finish", "clear"}:
                        if now < self._queue_retry_at:
                            self._queue_condition.wait(
                                min(self._queue_retry_at - now, 0.1)
                            )
                            continue
                        self._queue.popleft()
                        self._queue_bytes -= job.size
                        self._in_flight = True
                        self._in_flight_kind = job.kind
                        return job

                if self._pending_events:
                    event_id, event_job = next(iter(self._pending_events.items()))
                    retry_at = self._event_retry_at.get(event_id, 0.0)
                    if retry_at <= now:
                        self._in_flight = True
                        self._in_flight_kind = "event"
                        return event_job
                    if self._queue and self._queue[0].kind == "clear":
                        job = self._queue.popleft()
                        self._queue_bytes -= job.size
                        self._in_flight = True
                        self._in_flight_kind = job.kind
                        return job
                    self._queue_condition.wait(min(retry_at - now, 0.1))
                    continue

                if self._queue:
                    job = self._queue.popleft()
                    self._queue_bytes -= job.size
                    self._in_flight = True
                    self._in_flight_kind = job.kind
                    return job
                self._queue_condition.wait(0.1)

    async def _deliver(self, client: httpx.AsyncClient, job: _Job) -> bool:
        delay = 0.25
        recovered = False
        while not self._stop.is_set():
            try:
                await self._send_job(client, job)
                self._mark_connected()
                return True
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                detail = self._response_detail(exc.response)
                if status_code == 404 and not recovered and job.kind not in {
                    "experiment",
                    "architecture",
                }:
                    recovered = True
                    try:
                        await self._resynchronize(client)
                        if job.kind not in {"epoch", "event"}:
                            await self._send_job(client, job)
                        self._mark_connected()
                        return True
                    except (httpx.HTTPError, TransportError) as sync_error:
                        self._mark_disconnected(str(sync_error))
                elif status_code == 409 and job.kind == "epoch":
                    if await self._epoch_already_present(client, job.payload["epoch"]):
                        self._mark_connected()
                        return True
                    self._drop_permanent(job, detail)
                    return False
                elif status_code in (408, 425, 429) or status_code >= 500:
                    self._mark_disconnected(f"HTTP {status_code}: {detail}")
                else:
                    self._drop_permanent(job, f"HTTP {status_code}: {detail}")
                    return False
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._mark_disconnected(f"{type(exc).__name__}: {exc}")
            except (PayloadTooLargeError, TransportError) as exc:
                self._drop_permanent(job, str(exc))
                return False

            if self._stop.is_set():
                break
            logger.warning(
                "Backend unavailable; retrying %s in %.2f seconds: %s",
                job.kind,
                delay,
                self._last_error,
            )
            if await asyncio.to_thread(self._stop.wait, delay):
                break
            delay = min(delay * 2, self.config.retry_max_delay)
        return False

    async def _send_job(self, client: httpx.AsyncClient, job: _Job) -> None:
        if job.kind == "experiment":
            await self._request(
                client, "POST", "/api/v1/experiments", job.payload
            )
        elif job.kind == "architecture":
            await self._request(
                client,
                "POST",
                f"/api/v1/experiments/{self.experiment_id}/architecture",
                job.payload,
            )
        elif job.kind == "epoch":
            epoch = job.payload["epoch"]
            payload = self._epoch_provider(epoch)
            if payload is None:
                raise TransportError(
                    f"Epoch {epoch} left the bounded local window before delivery"
                )
            await self._send_epoch(client, payload)
        elif job.kind == "event":
            await self._request(
                client,
                "POST",
                f"/api/v1/experiments/{self.experiment_id}/events",
                job.payload,
            )
        elif job.kind == "finish":
            await self._request(
                client,
                "POST",
                f"/api/v1/experiments/{self.experiment_id}/finish",
            )
        elif job.kind == "clear":
            await self._request(
                client,
                "DELETE",
                f"/api/v1/experiments/{self.experiment_id}",
            )
        else:
            raise TransportError(f"Unknown transport operation: {job.kind}")

    async def _send_epoch(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> None:
        path = f"/api/v1/experiments/{self.experiment_id}/epochs"
        if self._payload_size(payload) <= self.config.max_request_bytes:
            await self._request(client, "POST", path, payload)
            return

        chunks = self._split_epoch(payload)
        chunk_path = (
            f"/api/v1/experiments/{self.experiment_id}/epochs/"
            f"{payload['epoch']}/chunks"
        )
        for index, neurons in enumerate(chunks):
            chunk = {
                "experiment_id": self.experiment_id,
                "chunk_index": index,
                "chunk_count": len(chunks),
                "neurons": neurons,
            }
            if self._payload_size(chunk) > self.config.max_request_bytes:
                raise PayloadTooLargeError(
                    "A compacted epoch chunk exceeds max_request_bytes"
                )
            await self._request(client, "POST", chunk_path, chunk)
        await self._request(
            client,
            "POST",
            f"/api/v1/experiments/{self.experiment_id}/epochs/"
            f"{payload['epoch']}/complete",
            {
                "experiment_id": self.experiment_id,
                "chunk_count": len(chunks),
            },
        )

    def _split_epoch(
        self, payload: dict[str, Any]
    ) -> list[dict[str, dict[str, Any]]]:
        by_layer: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for neuron_id, metrics in payload["neurons"].items():
            by_layer.setdefault(metrics["layer_id"], []).append(
                (neuron_id, metrics)
            )

        chunks: list[dict[str, dict[str, Any]]] = []
        for rows in by_layer.values():
            current: dict[str, dict[str, Any]] = {}
            for neuron_id, metrics in rows:
                candidate = {**current, neuron_id: metrics}
                probe = {
                    "experiment_id": self.experiment_id,
                    "epoch": payload["epoch"],
                    "chunk_index": 9999,
                    "chunk_count": 10000,
                    "neurons": candidate,
                }
                if self._payload_size(probe) > self.config.max_request_bytes:
                    if not current:
                        raise PayloadTooLargeError(
                            f"Neuron {neuron_id} exceeds max_request_bytes by itself"
                        )
                    chunks.append(current)
                    current = {neuron_id: metrics}
                    probe["neurons"] = current
                    if self._payload_size(probe) > self.config.max_request_bytes:
                        raise PayloadTooLargeError(
                            f"Neuron {neuron_id} exceeds max_request_bytes by itself"
                        )
                else:
                    current = candidate
            if current:
                chunks.append(current)

        if not chunks:
            raise PayloadTooLargeError(
                "Epoch exceeds max_request_bytes but contains no chunkable neurons"
            )
        return chunks

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(
            json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        )

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> httpx.Response:
        response = await client.request(
            method,
            self.endpoint + path,
            json=payload,
        )
        response.raise_for_status()
        return response

    async def _epoch_already_present(
        self, client: httpx.AsyncClient, epoch: int
    ) -> bool:
        try:
            response = await self._request(
                client,
                "GET",
                f"/api/v1/experiments/{self.experiment_id}/live",
            )
        except httpx.HTTPError:
            return False
        try:
            return int(response.json().get("current_epoch", -1)) >= epoch
        except (TypeError, ValueError):
            return False

    async def _resynchronize(self, client: httpx.AsyncClient) -> None:
        await self._request(
            client,
            "POST",
            "/api/v1/experiments",
            {
                "experiment_id": self.experiment_id,
                "name": self.experiment_name,
                "framework": "pytorch",
                "model_name": self.model_name,
            },
        )
        await self._request(
            client,
            "POST",
            f"/api/v1/experiments/{self.experiment_id}/architecture",
            self.architecture,
        )
        epochs, events = self._state_provider()
        for payload in epochs:
            try:
                await self._send_epoch(client, payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409 or not await self._epoch_already_present(
                    client, payload["epoch"]
                ):
                    raise
        for payload in events:
            await self._request(
                client,
                "POST",
                f"/api/v1/experiments/{self.experiment_id}/events",
                payload,
            )
        logger.info("Synchronized experiment %s with backend", self.experiment_id)

    def _mark_connected(self) -> None:
        with self._state_lock:
            self._connected = True
            self._last_error = None

    def _mark_disconnected(self, detail: str) -> None:
        with self._state_lock:
            self._connected = False
            self._last_error = detail

    def _record_error(self, detail: str) -> None:
        with self._state_lock:
            self._last_error = detail

    def _record_event_error(self, detail: str) -> None:
        with self._state_lock:
            self._last_event_error = detail
            self._last_error = detail

    def _count_drop(self, kind: str) -> None:
        with self._state_lock:
            self._dropped_messages += 1
            if kind == "epoch":
                self._dropped_epochs += 1
            elif kind == "event":
                self._dropped_events += 1

    def _drop_permanent(self, job: _Job, detail: str) -> None:
        if job.kind == "event":
            event_id = job.payload["event_id"]
            if event_id not in self._failed_event_ids:
                self._failed_event_ids.add(event_id)
                self._event_delivery_failures += 1
            self._record_event_error(
                f"Collapse event {event_id} delivery failed: {detail}"
            )
        else:
            self._record_error(
                f"{job.kind} delivery failed and remains queued: {detail}"
            )
        logger.error("Could not deliver %s message: %s", job.kind, detail)

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text[:300]
        if isinstance(body, dict):
            return str(body.get("detail", body))[:300]
        return str(body)[:300]
