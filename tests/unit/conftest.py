"""Shared unit test fixtures for the forward proxy.

The tests work with real loopback sockets rather than stream mocks: only
this way can we verify the real semantics of ``drain()``, connection
termination, and the delivery order of stream chunks.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest_asyncio


@dataclass(frozen=True, slots=True)
class ConnectedStreams:
    """A pair of connected ends of a TCP connection.

    Attributes:
        left_reader: the connection initiator's read stream.
        left_writer: the connection initiator's write stream.
        right_reader: the accepting side's read stream.
        right_writer: the accepting side's write stream.
    """

    left_reader: asyncio.StreamReader
    left_writer: asyncio.StreamWriter
    right_reader: asyncio.StreamReader
    right_writer: asyncio.StreamWriter


@pytest_asyncio.fixture
async def connect_streams() -> AsyncIterator[Callable[[], Awaitable[ConnectedStreams]]]:
    """Provide a factory of connected stream pairs on the loopback interface.

    Yields:
        A coroutine factory that creates a new pair each time; all created
        sockets and temporary servers are closed when the test finishes.
    """
    servers: list[asyncio.Server] = []
    writers: list[asyncio.StreamWriter] = []

    async def factory() -> ConnectedStreams:
        accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
        accepted = asyncio.get_running_loop().create_future()

        def on_connected(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            accepted.set_result((reader, writer))

        server = await asyncio.start_server(on_connected, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        left_reader, left_writer = await asyncio.open_connection("127.0.0.1", port)
        right_reader, right_writer = await accepted
        writers.extend([left_writer, right_writer])
        return ConnectedStreams(left_reader, left_writer, right_reader, right_writer)

    yield factory

    for writer in writers:
        writer.close()
    for server in servers:
        server.close()
        await server.wait_closed()
