from enum import Enum, auto
import http.client
import socket
import sys
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import keeper


class TunnelState(Enum):
    Invalid = auto()
    Stopped = auto()
    Ended = auto()
    ForwardingSucceeded = auto()


class KeeperTests(unittest.TestCase):
    def test_sdk_forwards_udp_to_the_selected_loopback_port(self):
        settings = keeper.read_settings({
            "PINGGY_TOKEN": "pinggy-example",
            "FREEDNS_TOKEN": "freedns-example",
            "WIREGUARD_PORT": "12345",
        })
        tunnel = Mock(
            state=TunnelState.ForwardingSucceeded,
            urls=["udp://example.test:54321"],
        )
        tunnel_factory = Mock(return_value=tunnel)
        modules = {
            "pinggy": SimpleNamespace(Tunnel=tunnel_factory),
            "pinggy.pylib": SimpleNamespace(TunnelState=TunnelState),
        }
        disconnected = Event()
        with patch.dict(sys.modules, modules):
            self.assertIs(keeper.start_tunnel(settings, Event(), disconnected), tunnel)
        tunnel_factory.assert_called_once_with(server_address="free.pinggy.io:443")
        tunnel.add_forwarding.assert_called_once_with("127.0.0.1:12345", type="udp")
        tunnel.start.assert_called_once_with(thread=True, block_until_ready=False)
        self.assertEqual(tunnel.webdebugger_port, 0)
        tunnel.on_disconnected("session closed")
        self.assertTrue(disconnected.is_set())

    def test_rotation_deadline_applies_even_during_dns_backoff(self):
        settings = keeper.read_settings({
            "PINGGY_TOKEN": "pinggy-example",
            "FREEDNS_TOKEN": "freedns-example",
            "POLL_INTERVAL_SECONDS": "3600",
        })
        old = Mock(state=TunnelState.ForwardingSucceeded, urls=["udp://example.test:11111"])
        new = Mock(state=TunnelState.ForwardingSucceeded, urls=["udp://example.test:22222"])
        stopped = Event()
        modules = {"pinggy.pylib": SimpleNamespace(TunnelState=TunnelState)}
        clock = 100.0
        waits = []

        def wait(delay):
            nonlocal clock
            waits.append(delay)
            clock += delay
            if len(waits) == 2:
                stopped.set()

        def start(_settings, _stopped, _disconnected):
            if not waits:
                return old
            old.stop.assert_called_once_with()
            self.assertEqual(clock, 100 + 55 * 60)
            return new

        with (
            patch.dict(sys.modules, modules),
            patch.object(keeper.time, "monotonic", side_effect=lambda: clock),
            patch.object(stopped, "wait", side_effect=wait),
            patch.object(keeper, "start_tunnel", side_effect=start),
            patch.object(keeper, "sync_endpoint", side_effect=[keeper.SyncError("DNS unavailable"), "2001::1"]) as sync,
            self.assertLogs(keeper.log, "INFO") as logs,
        ):
            keeper.run(settings, stopped)

        self.assertEqual(waits, [55 * 60, 55 * 60])
        self.assertTrue(any("after 55 minutes" in entry for entry in logs.output))
        sync.assert_called_with(new.urls, settings.freedns_update_url, None)
        new.stop.assert_called_once_with()

    def test_recreates_when_sdk_signals_disconnect_with_stale_active_state(self):
        settings = keeper.read_settings({"PINGGY_TOKEN": "pinggy-example", "FREEDNS_TOKEN": "freedns-example"})
        old = Mock(state=TunnelState.ForwardingSucceeded, urls=["udp://example.test:11111"])
        new = Mock(state=TunnelState.ForwardingSucceeded, urls=["udp://example.test:22222"])
        stopped = Event()
        modules = {"pinggy.pylib": SimpleNamespace(TunnelState=TunnelState)}
        sessions = []

        def start(_settings, _stopped, disconnected):
            sessions.append(disconnected)
            return old if len(sessions) == 1 else new

        def wait(_delay):
            if len(sessions) == 1:
                sessions[0].set()
            else:
                stopped.set()

        with (
            patch.dict(sys.modules, modules),
            patch.object(stopped, "wait", side_effect=wait),
            patch.object(keeper, "start_tunnel", side_effect=start),
            patch.object(keeper, "sync_endpoint", side_effect=["2001::1", "2001::2"]) as sync,
            self.assertLogs(keeper.log, "WARNING") as logs,
        ):
            keeper.run(settings, stopped)

        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertTrue(any("disconnected; reconnecting" in entry for entry in logs.output))
        sync.assert_called_with(new.urls, settings.freedns_update_url, "2001::1")
        old.stop.assert_called_once_with()
        new.stop.assert_called_once_with()

    def test_ip4p_and_udp_selection(self):
        self.assertEqual(keeper.encode_ip4p("203.0.113.8", 51820), "2001::ca6c:cb00:7108")
        self.assertEqual(keeper.encode_ip4p("192.0.2.1", 65535), "2001::ffff:c000:201")
        self.assertEqual(
            keeper.udp_endpoint(["tcp://example.test:80", "udp://example.test:43210"]),
            ("example.test", 43210),
        )
        with self.assertRaises(keeper.SyncError):
            keeper.udp_endpoint(["udp://[2001:db8::1]:51820", "udp://example.test:0"])

    def test_polling_detects_ip_and_port_changes_and_retries_failed_updates(self):
        ipv4 = "203.0.113.8"
        reject_update = True
        updates = []

        def resolve(host, port, family, socktype):
            self.assertEqual((host, family, socktype), ("example.test", socket.AF_INET, socket.SOCK_DGRAM))
            return [(family, socktype, 17, "", (ipv4, 0))]

        def request(method, resource):
            self.assertEqual(method, "GET")
            parsed = urlsplit(resource)
            self.assertEqual(parsed.path, "/u/example-token/")
            address = parse_qs(parsed.query)["ip"][0]
            updates.append(address)
            if reject_update:
                connection.getresponse.return_value.read.return_value = b"ERROR: invalid token"
            else:
                body = f"Updated example.test from 2001:db8::1 to {address}"
                connection.getresponse.return_value.read.return_value = body.encode()

        connection = Mock()
        connection.getresponse.return_value.status = 200
        connection.request.side_effect = request
        urls = ["udp://example.test:51820"]
        settings = keeper.read_settings({
            "PINGGY_TOKEN": "pinggy-example",
            "FREEDNS_TOKEN": "example-token",
        })
        update_url = settings.freedns_update_url
        with (
            patch.object(socket, "getaddrinfo", side_effect=resolve),
            patch.object(http.client, "HTTPSConnection", return_value=connection) as https,
        ):
            with self.assertRaisesRegex(keeper.SyncError, "did not confirm"):
                keeper.sync_endpoint(urls, update_url)
            reject_update = False
            initial = keeper.sync_endpoint(urls, update_url)
            self.assertEqual(keeper.sync_endpoint(urls, update_url, initial), initial)
            urls = ["udp://example.test:51821"]
            port_change = keeper.sync_endpoint(urls, update_url, initial)
            ipv4 = "203.0.113.9"
            host_change = keeper.sync_endpoint(urls, update_url, port_change)
        self.assertEqual(port_change, "2001::ca6d:cb00:7108")
        self.assertEqual(host_change, "2001::ca6d:cb00:7109")
        self.assertEqual(updates, [initial, initial, port_change, host_change])
        https.assert_called_with("sync.afraid.org", timeout=8)

    def test_v1_direct_url_publishes_the_encoded_aaaa_address(self):
        settings = keeper.read_settings({
            "PINGGY_TOKEN": "pinggy-example",
            "FREEDNS_UPDATE_URL": "https://freedns.afraid.org/dynamic/update.php?example+/token==",
            "FREEDNS_TOKEN": "old-example-token",
        })
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        address = "2001::ca6c:cb00:7108"

        with patch.object(http.client, "HTTPSConnection", return_value=connection) as https:
            response.read.return_value = b"Updated wg.example.test to 2001::CA6C:CB00:7108 in 0.041 seconds\n"
            keeper.update_freedns(settings.freedns_update_url, address)
            https.assert_called_once_with("freedns.afraid.org", timeout=8)
            connection.request.assert_called_once_with(
                "GET", "/dynamic/update.php?example+/token==&address=2001%3A%3Aca6c%3Acb00%3A7108",
            )

            response.read.return_value = b"ERROR: Address 2001::ca6c:cb00:7108 has not changed.\n"
            keeper.update_freedns(settings.freedns_update_url, address)
            with self.assertRaisesRegex(keeper.SyncError, "did not confirm"):
                keeper.update_freedns(settings.freedns_update_url, "2001::ca6d:cb00:7108")
