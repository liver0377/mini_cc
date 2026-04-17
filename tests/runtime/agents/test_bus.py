from __future__ import annotations

from mini_cc.runtime.agents import AgentEventBus, AgentLifecycleEvent


class TestAgentEventBus:
    def test_publish_and_drain(self) -> None:
        bus = AgentEventBus()
        bus.publish_nowait(AgentLifecycleEvent(event_type="created", agent_id="a1", readonly=True))
        bus.publish_nowait(AgentLifecycleEvent(event_type="completed", agent_id="a1", success=True))

        events = bus.drain()

        assert len(events) == 2
        assert events[0].event_type == "created"
        assert events[0].agent_id == "a1"
        assert events[0].readonly is True
        assert events[1].event_type == "completed"
        assert events[1].success is True

    def test_drain_empty(self) -> None:
        bus = AgentEventBus()
        assert bus.drain() == []

    def test_drain_consumes_all(self) -> None:
        bus = AgentEventBus()
        for i in range(5):
            bus.publish_nowait(AgentLifecycleEvent(event_type="created", agent_id=f"a{i}"))

        first = bus.drain()
        assert len(first) == 5

        second = bus.drain()
        assert len(second) == 0

    async def test_async_publish(self) -> None:
        bus = AgentEventBus()
        await bus.publish(AgentLifecycleEvent(event_type="created", agent_id="a1"))
        events = bus.drain()
        assert len(events) == 1
