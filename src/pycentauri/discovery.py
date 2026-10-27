"""LAN discovery for Elegoo Centauri Carbon printers.

Both models answer a UDP broadcast with a JSON payload describing
themselves: the original Centauri Carbon (CC1) listens on port 3000 for
the magic probe string ``M99999``, the Centauri Carbon 2 (CC2) listens on
port 52700 for a JSON-RPC probe ``{"id": 0, "method": 7000}`` and answers
with its serial number (approach from upstream bjan/pycentauri PR #1,
tested against a real CC2).
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from typing import Any

DISCOVERY_PORT = 3000
DISCOVERY_PROBE = b"M99999"
DISCOVERY_PORT_CC2 = 52700
DISCOVERY_PROBE_CC2 = json.dumps({"id": 0, "method": 7000}).encode("utf-8")
DEFAULT_TIMEOUT = 3.0


@dataclass(slots=True)
class DiscoveredPrinter:
    """A printer that answered a discovery broadcast."""

    host: str
    mainboard_id: str | None
    name: str | None
    machine_name: str | None
    firmware_version: str | None
    raw: dict[str, Any]
    protocol: str = "cc1"  # "cc1" (SDCP M99999) or "cc2" (JSON-RPC :52700)
    serial_number: str | None = None  # CC2 only ("sn")
    lan_status: int | None = None  # CC2 only: 1 = LAN-only mode, 0 = cloud mode

    @property
    def is_cc2(self) -> bool:
        return self.protocol == "cc2"


def _parse_response(data: bytes, host: str) -> DiscoveredPrinter | None:
    try:
        obj = json.loads(data.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    result_raw = obj.get("result")
    if isinstance(result_raw, dict) and "sn" in result_raw:
        # CC2 shape: JSON-RPC result with the serial number.
        return DiscoveredPrinter(
            host=host,
            mainboard_id=None,
            name=result_raw.get("host_name"),
            machine_name=result_raw.get("machine_model"),
            firmware_version=None,
            raw=obj,
            protocol="cc2",
            serial_number=result_raw.get("sn"),
            lan_status=result_raw.get("lan_status"),
        )
    inner_raw = obj.get("Data")
    inner: dict[str, Any] = inner_raw if isinstance(inner_raw, dict) else {}
    return DiscoveredPrinter(
        host=host,
        mainboard_id=inner.get("MainboardID") or obj.get("MainboardID"),
        name=inner.get("Name"),
        machine_name=inner.get("MachineName"),
        firmware_version=inner.get("FirmwareVersion"),
        raw=obj,
    )


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.results: dict[str, DiscoveredPrinter] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        host = addr[0]
        if host in self.results:
            return
        parsed = _parse_response(data, host)
        if parsed is not None:
            self.results[host] = parsed


async def discover(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    broadcast_address: str = "255.255.255.255",
    port: int = DISCOVERY_PORT,
    port_cc2: int = DISCOVERY_PORT_CC2,
    retries: int = 3,
) -> list[DiscoveredPrinter]:
    """Broadcast both discovery probes and collect responders.

    Blocks for ``timeout`` seconds. Sends the CC1 ``M99999`` probe to
    ``port`` and the CC2 JSON-RPC probe to ``port_cc2`` (from one socket),
    then returns one entry per responding printer, de-duplicated by source
    IP. The probes are retransmitted ``retries`` times at evenly-spaced
    intervals within the timeout window, since UDP probes can be dropped
    on busy or congested networks. Safe to call concurrently from multiple
    tasks; each call uses its own UDP socket.
    """
    loop = asyncio.get_running_loop()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.setblocking(False)

    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        sock=sock,
    )
    try:
        tries = max(1, retries)
        interval = timeout / max(tries, 1) / 2
        for _ in range(tries):
            transport.sendto(DISCOVERY_PROBE, (broadcast_address, port))
            transport.sendto(DISCOVERY_PROBE_CC2, (broadcast_address, port_cc2))
            await asyncio.sleep(interval)
        # Listen for the remainder of the budget for late replies.
        remaining = max(0.0, timeout - interval * tries)
        if remaining:
            await asyncio.sleep(remaining)
    finally:
        transport.close()

    return list(protocol.results.values())
