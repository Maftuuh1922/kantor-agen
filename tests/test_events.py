import asyncio

import pytest

from skeleton.core.events import EventBus, SubscriptionClosed


async def test_fan_out_to_multiple_subscribers():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish({"type": "x", "n": 1})
    assert await a.get() == {"type": "x", "n": 1}
    assert await b.get() == {"type": "x", "n": 1}


async def test_get_waits_for_publish():
    bus = EventBus()
    sub = bus.subscribe()
    task = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    assert not task.done()
    bus.publish({"type": "late"})
    assert await asyncio.wait_for(task, 1) == {"type": "late"}


async def test_late_subscriber_misses_earlier_events():
    bus = EventBus()
    bus.publish({"type": "old"})
    sub = bus.subscribe()
    bus.publish({"type": "new"})
    assert (await sub.get())["type"] == "new"


async def test_slow_subscriber_drops_oldest_without_blocking():
    bus = EventBus()
    slow = bus.subscribe(maxsize=2)
    fast = bus.subscribe(maxsize=10)
    for i in range(5):
        bus.publish({"type": "t", "i": i})  # must not block or raise
    assert [(await slow.get())["i"] for _ in range(2)] == [3, 4]
    assert [(await fast.get())["i"] for _ in range(5)] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("bad", [{}, {"data": 1}, None, "x"])
def test_event_without_type_rejected(bad):
    with pytest.raises(ValueError):
        EventBus().publish(bad)


async def test_close_stops_delivery_and_unsubscribes():
    bus = EventBus()
    sub = bus.subscribe()
    other = bus.subscribe()
    bus.publish({"type": "before"})
    sub.close()
    sub.close()  # idempotent
    assert bus.subscriber_count == 1
    bus.publish({"type": "after"})
    with pytest.raises(SubscriptionClosed):
        await sub.get()
    assert [(await other.get())["type"] for _ in range(2)] == ["before", "after"]


async def test_close_wakes_waiting_getter():
    bus = EventBus()
    sub = bus.subscribe()
    task = asyncio.create_task(sub.get())
    await asyncio.sleep(0)
    sub.close()
    with pytest.raises(SubscriptionClosed):
        await asyncio.wait_for(task, 1)


async def test_unsubscribe_via_bus_and_async_iteration():
    bus = EventBus()
    sub = bus.subscribe()
    bus.publish({"type": "a"})
    bus.publish({"type": "b"})
    seen = []
    async for event in sub:
        seen.append(event["type"])
        if len(seen) == 2:
            bus.unsubscribe(sub)
    assert seen == ["a", "b"]
    assert sub.closed


async def test_subscribers_get_independent_copies():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    bus.publish({"type": "x", "v": 1})
    (await a.get())["v"] = 99
    assert (await b.get())["v"] == 1


def test_invalid_maxsize_rejected():
    with pytest.raises(ValueError):
        EventBus().subscribe(maxsize=0)
