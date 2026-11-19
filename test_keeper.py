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


class KeeperTests(unittest.TestCase):
    def test_sdk_forwards_udp_to_the_selected_loopback_port(self):
        class TunnelState(Enum):
            Invalid = auto()
            Stopped = auto()
            Ended = auto()
            ForwardingSucceeded = auto()

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
        with patch.dict(sys.modules, modules):
            self.assertIs(keeper.start_tunnel(settings, Event()), tunnel)
        tunnel_factory.assert_called_once_with(server_address="free.pinggy.io:443")
        tunnel.add_forwarding.assert_called_once_with("127.0.0.1:12345", type="udp")
        tunnel.start.assert_called_once_with(thread=True, block_until_ready=False)
        self.assertEqual(tunnel.webdebugger_port, 0)

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
        with (
            patch.object(socket, "getaddrinfo", side_effect=resolve),
            patch.object(http.client, "HTTPSConnection", return_value=connection) as https,
        ):
            with self.assertRaisesRegex(keeper.SyncError, "did not confirm"):
                keeper.sync_endpoint(urls, "example-token")
            reject_update = False
            initial = keeper.sync_endpoint(urls, "example-token")
            self.assertEqual(keeper.sync_endpoint(urls, "example-token", initial), initial)
            urls = ["udp://example.test:51821"]
            port_change = keeper.sync_endpoint(urls, "example-token", initial)
            ipv4 = "203.0.113.9"
            host_change = keeper.sync_endpoint(urls, "example-token", port_change)
        self.assertEqual(port_change, "2001::ca6d:cb00:7108")
        self.assertEqual(host_change, "2001::ca6d:cb00:7109")
        self.assertEqual(updates, [initial, initial, port_change, host_change])
        https.assert_called_with("sync.afraid.org", timeout=8)
