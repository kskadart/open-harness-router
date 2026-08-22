"""Unit tests for the forward proxy's blind tunnel (``proxy.streams``)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from proxy.streams import close_stream, pump_tunnel
from unit.conftest import ConnectedStreams

StreamFactory = Callable[[], Awaitable[ConnectedStreams]]

_READ_TIMEOUT_S = 2.0
# The gap between chunks in tunnel tests that don't test the idle timeout --
# deliberately larger than the test's runtime so it never triggers by accident.
_IDLE_TIMEOUT_S = 5.0


async def test_tunnel_forwards_bytes_from_client_to_upstream(
    connect_streams: StreamFactory,
) -> None:
    """Client bytes reach the target unchanged."""
    client_side = await connect_streams()
    upstream_side = await connect_streams()
    tunnel = asyncio.create_task(
        pump_tunnel(
            client_side.right_reader,
            client_side.right_writer,
            upstream_side.left_reader,
            upstream_side.left_writer,
            _IDLE_TIMEOUT_S,
        )
    )

    client_side.left_writer.write(b"\x16\x03\x01client-hello")
    await client_side.left_writer.drain()
    received = await asyncio.wait_for(
        upstream_side.right_reader.readexactly(15), timeout=_READ_TIMEOUT_S
    )

    assert received == b"\x16\x03\x01client-hello"
    tunnel.cancel()


async def test_tunnel_forwards_bytes_from_upstream_to_client(
    connect_streams: StreamFactory,
) -> None:
    """Target bytes reach the client unchanged."""
    client_side = await connect_streams()
    upstream_side = await connect_streams()
    tunnel = asyncio.create_task(
        pump_tunnel(
            client_side.right_reader,
            client_side.right_writer,
            upstream_side.left_reader,
            upstream_side.left_writer,
            _IDLE_TIMEOUT_S,
        )
    )

    upstream_side.right_writer.write(b"\x16\x03\x03server-hello")
    await upstream_side.right_writer.drain()
    received = await asyncio.wait_for(
        client_side.left_reader.readexactly(15), timeout=_READ_TIMEOUT_S
    )

    assert received == b"\x16\x03\x03server-hello"
    tunnel.cancel()


async def test_tunnel_closes_both_sides_when_client_disconnects(
    connect_streams: StreamFactory,
) -> None:
    """A client closing the connection finishes the tunnel and closes the target connection."""
    client_side = await connect_streams()
    upstream_side = await connect_streams()
    tunnel = asyncio.create_task(
        pump_tunnel(
            client_side.right_reader,
            client_side.right_writer,
            upstream_side.left_reader,
            upstream_side.left_writer,
            _IDLE_TIMEOUT_S,
        )
    )
    await asyncio.sleep(0)

    client_side.left_writer.close()
    await asyncio.wait_for(tunnel, timeout=_READ_TIMEOUT_S)

    assert await asyncio.wait_for(upstream_side.right_reader.read(), timeout=_READ_TIMEOUT_S) == b""


async def test_close_stream_is_idempotent(connect_streams: StreamFactory) -> None:
    """Closing a stream twice does not raise an error."""
    streams = await connect_streams()

    await close_stream(streams.left_writer)
    await close_stream(streams.left_writer)

    assert streams.left_writer.is_closing()


async def test_tunnel_closes_on_silence_longer_than_the_idle_timeout(
    connect_streams: StreamFactory,
) -> None:
    """Silence in the tunnel longer than the idle timeout closes both sides."""
    client_side = await connect_streams()
    upstream_side = await connect_streams()

    tunnel = asyncio.create_task(
        pump_tunnel(
            client_side.right_reader,
            client_side.right_writer,
            upstream_side.left_reader,
            upstream_side.left_writer,
            0.05,
        )
    )

    await asyncio.wait_for(tunnel, timeout=_READ_TIMEOUT_S)

    assert await asyncio.wait_for(client_side.left_reader.read(), timeout=_READ_TIMEOUT_S) == b""
    assert (
        await asyncio.wait_for(upstream_side.right_reader.read(), timeout=_READ_TIMEOUT_S) == b""
    )


async def test_tunnel_survives_silence_shorter_than_the_idle_timeout(
    connect_streams: StreamFactory,
) -> None:
    """Chunks less often than the overall timeout but more often than idle -- the tunnel survives.

    Bytes flow in BOTH directions on every iteration: the idle timeout in
    ``pump_tunnel`` is counted independently per direction, and silence in
    either one already breaks the whole tunnel.
    """
    client_side = await connect_streams()
    upstream_side = await connect_streams()
    tunnel = asyncio.create_task(
        pump_tunnel(
            client_side.right_reader,
            client_side.right_writer,
            upstream_side.left_reader,
            upstream_side.left_writer,
            0.2,
        )
    )

    for _ in range(3):
        await asyncio.sleep(0.08)
        client_side.left_writer.write(b"x")
        await client_side.left_writer.drain()
        await asyncio.wait_for(upstream_side.right_reader.readexactly(1), timeout=_READ_TIMEOUT_S)
        upstream_side.right_writer.write(b"y")
        await upstream_side.right_writer.drain()
        await asyncio.wait_for(client_side.left_reader.readexactly(1), timeout=_READ_TIMEOUT_S)

    assert not tunnel.done()
    tunnel.cancel()
