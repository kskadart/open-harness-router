"""Low-level operations on ``StreamReader``/``StreamWriter`` pairs.

This is where operations on raw forward-proxy connections live that are
independent of HTTP: a blind bidirectional tunnel for hosts outside the
allowlist, and correct stream closing. Establishing outbound connections
(directly or via an upstream proxy) lives in ``proxy.outbound``, parsing
HTTP on top of a decrypted connection -- in ``proxy.session``.
"""

from __future__ import annotations

import asyncio
import ssl

from log import get_logger

logger = get_logger(__name__)

# Chunk size when pumping bytes through the tunnel. 64 KiB roughly matches
# the upper bound of a TCP window on a local connection: smaller means
# extra trips into the event loop, larger means wasted memory held per
# connection.
_TUNNEL_CHUNK_BYTES = 64 * 1024


async def close_stream(writer: asyncio.StreamWriter) -> None:
    """Close a write stream without letting a close failure disrupt handling.

    Args:
        writer: the stream to close; a repeat call is safe.
    """
    if writer.is_closing():
        return
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, ssl.SSLError):
        # The connection was already torn down by the other side -- there
        # is nothing left to finish for TLS or TCP, the stream's state is
        # already final.
        return


async def _copy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, idle_timeout_s: float
) -> None:
    """Pump bytes from a read stream to a write stream until data ends.

    Without a limit on silence between chunks, a side that goes quiet mid-
    tunnel (without closing the connection) would hold the task and both
    sockets forever. The limit is on idle time between chunks specifically,
    not on the duration of the whole transfer: a legitimately long tunnel
    must not be cut off while bytes keep flowing.

    Args:
        reader: source of bytes.
        writer: destination of bytes.
        idle_timeout_s: maximum silence between chunks.
    """
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(_TUNNEL_CHUNK_BYTES), timeout=idle_timeout_s
                )
            except TimeoutError:
                logger.warning("proxy_tunnel_idle_timeout", idle_timeout_s=idle_timeout_s)
                return
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
    except (OSError, ssl.SSLError):
        # Either side dropping is a normal end of the tunnel: the proxy has
        # no right to inspect the content, so there is nothing to recover.
        return


async def pump_tunnel(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    idle_timeout_s: float,
) -> None:
    """Pump bytes between the client and the target until either side closes.

    The content is not parsed or decrypted: for hosts outside the
    allowlist, the MITM proxy stays blind.

    Args:
        client_reader: read stream from the client.
        client_writer: write stream to the client.
        upstream_reader: read stream from the target.
        upstream_writer: write stream to the target.
        idle_timeout_s: maximum silence between chunks in either direction.
    """
    tasks = [
        asyncio.create_task(_copy(client_reader, upstream_writer, idle_timeout_s)),
        asyncio.create_task(_copy(upstream_reader, client_writer, idle_timeout_s)),
    ]
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        # Cancelled tasks are awaited here: without this, the event loop
        # would report an unfinished task after the connection is closed.
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await close_stream(upstream_writer)
        await close_stream(client_writer)
