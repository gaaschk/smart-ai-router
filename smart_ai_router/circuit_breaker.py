"""Process-local provider availability circuits; independent of semantic model health."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import threading
import time

import httpx


DEFAULTS = {'openrouter': (5, 60), 'ollama': (3, 30), 'bedrock': (10, 120)}


def provider_for(model):
    prefix = model.split('/', 1)[0]
    return prefix if prefix in DEFAULTS else 'openrouter'


@dataclass
class _State:
    failures: int = 0
    opened_at: float | None = None
    probe: bool = False
    generation: int = 0
    rejected: int = 0
    failed_attempts: int = 0
    successful_attempts: int = 0


class ProviderCircuits:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.states = {key: _State() for key in DEFAULTS}

    def blocked(self, provider, recovery):
        with self.lock:
            state = self.states[provider]
            return state.probe or (state.opened_at is not None and self.clock() - state.opened_at < recovery)

    def acquire(self, provider, recovery):
        with self.lock:
            state = self.states[provider]
            if state.probe or (state.opened_at is not None and self.clock() - state.opened_at < recovery):
                state.rejected += 1
                raise httpx.ConnectError(f'Provider circuit is open: {provider}')
            if state.opened_at is not None:
                state.probe = True
            return state.generation, state.probe

    def finish(self, provider, lease, threshold, outcome):
        """outcome: True=reachable, False=availability failure, None=cancelled."""
        with self.lock:
            state = self.states[provider]
            generation, probe = lease
            if generation != state.generation:
                return  # stale concurrent completion cannot close an opened circuit
            if outcome is None:
                if probe:
                    state.probe = False
                return
            if outcome:
                state.successful_attempts += 1
                state.failures = 0
                if probe:
                    state.opened_at = None
                    state.probe = False
                    state.generation += 1
            else:
                state.failed_attempts += 1
                state.failures += 1
                if probe or state.failures >= threshold:
                    state.opened_at = self.clock()
                    state.probe = False
                    state.generation += 1

    def snapshot(self, policy):
        with self.lock:
            now = self.clock()
            return {provider: {
                'state': ('half_open' if state.probe or (state.opened_at is not None and
                          now - state.opened_at >= policy[provider][1]) else
                          'open' if state.opened_at is not None else 'closed'),
                'consecutive_failures': state.failures,
                'failed_attempts': state.failed_attempts,
                'successful_attempts': state.successful_attempts,
                'rejected_attempts': state.rejected,
                'failure_threshold': policy[provider][0], 'recovery_seconds': policy[provider][1],
            } for provider, state in self.states.items()}


class CircuitClient:
    """Wrap only dispatch, so no lease is held while profiling or validating input."""
    def __init__(self, client, circuits, provider, policy):
        self.client, self.circuits, self.provider, self.policy = client, circuits, provider, policy

    def _lease(self):
        provider = self.provider()
        threshold, recovery = self.policy[provider]
        return provider, threshold, self.circuits.acquire(provider, recovery)

    @staticmethod
    def _reachable(status):
        return status != 429 and status < 500

    async def post(self, *args, **kwargs):
        provider, threshold, lease = self._lease()
        outcome = None
        try:
            response = await self.client.post(*args, **kwargs)
            outcome = self._reachable(response.status_code)
            return response
        except httpx.RequestError:
            outcome = False
            raise
        finally:
            self.circuits.finish(provider, lease, threshold, outcome)

    @asynccontextmanager
    async def stream(self, *args, **kwargs):
        provider, threshold, lease = self._lease()
        recorded = False
        try:
            async with self.client.stream(*args, **kwargs) as response:
                if response.status_code >= 400:
                    self.circuits.finish(provider, lease, threshold, self._reachable(response.status_code))
                    recorded = True  # update before proxy chooses its fallback
                yield response
                if not recorded:
                    self.circuits.finish(provider, lease, threshold, True)
                    recorded = True
        except httpx.RequestError:
            if not recorded:
                self.circuits.finish(provider, lease, threshold, False)
                recorded = True
            raise
        finally:
            if not recorded:
                self.circuits.finish(provider, lease, threshold, None)
