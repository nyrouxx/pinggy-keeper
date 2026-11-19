from contextlib import closing
from dataclasses import dataclass
import http.client
import ipaddress
import logging
import os
import re
import signal
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit


log = logging.getLogger("pinggy-keeper")
IP4P_PREFIX = ipaddress.IPv6Address("2001::").packed[:10]
ENDPOINT = re.compile(r"(udp://)?([a-z0-9.-]+):([0-9]{1,5})/?", re.IGNORECASE)
FREEDNS_V1_URL = re.compile(
    r"https?://freedns\.afraid\.org/dynamic/update\.php\?"
    r"(?:[A-Za-z0-9+/_-]|%2[BbFf])+(?:=|%3[Dd]){0,2}"
)
FREEDNS_V2_URL = re.compile(r"https?://(?:v6\.)?sync\.afraid\.org/u/[A-Za-z0-9_-]+/")


class SyncError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    pinggy_token: str
    pinggy_server: str
    freedns_update_url: str
    wireguard_port: int
    poll_seconds: int


def read_integer(env, name, default, maximum):
    raw = env.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw) or not 1 <= int(raw) <= maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return int(raw)


def read_settings(env):
    pinggy_token = env.get("PINGGY_TOKEN", "").strip()
    freedns_update_url = env.get("FREEDNS_UPDATE_URL", "").strip()
    pinggy_server = env.get("PINGGY_SERVER", "free.pinggy.io").strip()
    if not pinggy_token:
        raise ValueError("Set PINGGY_TOKEN to the access token from Pinggy")
    if not freedns_update_url:
        freedns_token = env.get("FREEDNS_TOKEN", "").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", freedns_token):
            freedns_update_url = f"https://sync.afraid.org/u/{freedns_token}/"
    if not (FREEDNS_V1_URL.fullmatch(freedns_update_url) or FREEDNS_V2_URL.fullmatch(freedns_update_url)):
        raise ValueError("Set FREEDNS_UPDATE_URL to the FreeDNS update link for the AAAA record")
    if not re.fullmatch(r"[A-Za-z0-9.-]+", pinggy_server):
        raise ValueError("PINGGY_SERVER must be a hostname, such as free.pinggy.io")
    return Settings(
        pinggy_token=pinggy_token,
        pinggy_server=pinggy_server,
        freedns_update_url=freedns_update_url,
        wireguard_port=read_integer(env, "WIREGUARD_PORT", 51820, 65535),
        poll_seconds=read_integer(env, "POLL_INTERVAL_SECONDS", 10, 3600),
    )


def encode_ip4p(ipv4, port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("IP4P requires a UDP port from 1 to 65535")
    payload = IP4P_PREFIX + port.to_bytes(2, "big") + ipaddress.IPv4Address(ipv4).packed
    return str(ipaddress.IPv6Address(payload))


def udp_endpoint(urls):
    bare_endpoint = None
    for url in urls:
        match = ENDPOINT.fullmatch(url.strip())
        if not match:
            continue
        scheme, host, port = match.groups()
        port = int(port)
        if not 1 <= port <= 65535:
            continue
        if scheme:
            return host, port
        if bare_endpoint is None:
            bare_endpoint = (host, port)
    if bare_endpoint is None:
        raise SyncError("Pinggy has no IPv4-capable UDP endpoint yet")
    return bare_endpoint


def resolve_ipv4(host, port, previous):
    try:
        results = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        raise SyncError(f"Pinggy hostname {host} has no reachable DNS A record") from None
    addresses = sorted({result[4][0] for result in results})
    if not addresses:
        raise SyncError(f"Pinggy hostname {host} has no DNS A record")
    return next((ip for ip in addresses if encode_ip4p(ip, port) == previous), addresses[0])


def update_freedns(update_url, address):
    url = urlsplit(update_url)
    is_v1 = url.path == "/dynamic/update.php"
    if is_v1:
        query = url.query + "&" + urlencode({"address": address})
    else:
        query = urlencode({"ip": address})
    try:
        with closing(http.client.HTTPSConnection(url.hostname, timeout=8)) as connection:
            connection.request("GET", f"{url.path}?{query}")
            response = connection.getresponse()
            if response.status != 200:
                raise SyncError(f"FreeDNS returned HTTP {response.status}")
            body = response.read(4096).decode("utf-8", errors="replace").strip()
    except (OSError, http.client.HTTPException):
        raise SyncError("FreeDNS request failed or timed out") from None

    if is_v1:
        result = re.fullmatch(
            r"Updated\s+[^\r\n]+?\s+to\s+([0-9a-f:.]+?)(?:\s+in\s+[0-9.]+\s+seconds?)?\.?",
            body,
            re.IGNORECASE,
        ) or re.fullmatch(r"ERROR:\s+Address\s+(\S+)\s+has not changed\.?", body, re.IGNORECASE)
    else:
        if re.match(r"No IP change detected\b", body, re.IGNORECASE):
            return
        result = re.fullmatch(r"Updated\s+\S+\s+from\s+\S+\s+to\s+(\S+)", body, re.IGNORECASE)
    if result:
        try:
            if str(ipaddress.IPv6Address(result[1])) == address:
                return
        except ValueError:
            pass
    raise SyncError("FreeDNS did not confirm the AAAA update. Check the update link for the AAAA record")


def sync_endpoint(urls, update_url, previous=None):
    host, port = udp_endpoint(urls)
    ipv4 = resolve_ipv4(host, port, previous)
    address = encode_ip4p(ipv4, port)
    if address != previous:
        update_freedns(update_url, address)
        log.info("FreeDNS AAAA %s (%s:%s)", address, ipv4, port)
    return address


def start_tunnel(settings, stopped):
    from pinggy import Tunnel
    from pinggy.pylib import TunnelState

    tunnel = Tunnel(server_address=f"{settings.pinggy_server}:443")
    ready = False
    try:
        tunnel.token = settings.pinggy_token
        tunnel.webdebugger_port = 0
        tunnel.auto_reconnect = False
        tunnel.add_forwarding(f"127.0.0.1:{settings.wireguard_port}", type="udp")
        tunnel.start(thread=True, block_until_ready=False)
        deadline = time.monotonic() + 60
        final_states = {TunnelState.Invalid, TunnelState.Stopped, TunnelState.Ended}
        while not stopped.is_set():
            state = tunnel.state
            if state is TunnelState.ForwardingSucceeded and tunnel.urls:
                ready = True
                return tunnel
            if state in final_states or time.monotonic() >= deadline:
                raise SyncError("Pinggy did not establish a UDP tunnel")
            stopped.wait(0.25)
        return None
    finally:
        if not ready:
            tunnel.stop()


def run(settings, stopped):
    from pinggy.pylib import TunnelState

    tunnel = None
    last_published = None
    backoff = settings.poll_seconds
    log.info("Forwarding UDP to 127.0.0.1:%s", settings.wireguard_port)
    try:
        while not stopped.is_set():
            try:
                if tunnel is not None and tunnel.state is not TunnelState.ForwardingSucceeded:
                    tunnel.stop()
                    tunnel = None
                if tunnel is None:
                    tunnel = start_tunnel(settings, stopped)
                    if tunnel is None:
                        break
                    log.info("Pinggy UDP tunnel established")
                last_published = sync_endpoint(tunnel.urls, settings.freedns_update_url, last_published)
            except SyncError as error:
                log.warning("%s. Retrying in %ss", error, backoff)
                stopped.wait(backoff)
                backoff = min(backoff * 2, max(settings.poll_seconds, 300))
            else:
                backoff = settings.poll_seconds
                stopped.wait(settings.poll_seconds)
    finally:
        if tunnel is not None:
            tunnel.stop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = read_settings(os.environ)
    except ValueError as error:
        log.error("%s", error)
        return 1

    stopped = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    run(settings, stopped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
