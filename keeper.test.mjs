import assert from 'node:assert/strict';
import { Resolver } from 'node:dns/promises';
import { once } from 'node:events';
import { createServer } from 'node:http';
import test from 'node:test';
import { encodeIp4p, syncEndpoint, udpEndpoint } from './keeper.mjs';

test('IP4P encodes the port and IPv4 in network byte order', () => {
  assert.equal(encodeIp4p('203.0.113.8', 51820), '2001::ca6c:cb00:7108');
  assert.equal(encodeIp4p('192.0.2.1', 65535), '2001::ffff:c000:201');
  assert.throws(() => encodeIp4p('::1', 51820));
  assert.throws(() => encodeIp4p('192.0.2.1', 0));
});

test('Pinggy addresses select the UDP endpoint and require a usable port', () => {
  assert.deepEqual(udpEndpoint({ urls: ['tcp://example.com:80', 'udp://example.com:43210'] }), {
    host: 'example.com',
    port: 43210,
  });
  assert.throws(() => udpEndpoint({ urls: ['udp://example.com:0', 'udp://[2001:db8::1]:53'] }));
});

test('polling retries failed updates and detects port and DNS A changes', async (t) => {
  let port = 51820;
  let ipv4 = '203.0.113.8';
  t.mock.method(Resolver.prototype, 'resolve4', async (host) => {
    assert.equal(host, 'example.test');
    return [ipv4];
  });
  const server = createServer((request, response) => {
    assert.equal(request.url, '/urls');
    response.end(JSON.stringify({ urls: [`udp://example.test:${port}`] }));
  });
  t.after(() => server.close());
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');

  const fakeToken = 'example-token';
  const pinggyUrl = new URL(`http://127.0.0.1:${server.address().port}/urls`);
  const config = { pinggyUrl, freednsToken: fakeToken };
  const updates = [];
  const fetchLocal = globalThis.fetch;
  let rejectUpdate = true;
  t.mock.method(globalThis, 'fetch', (url, options) => {
    if (url.hostname === '127.0.0.1') return fetchLocal(url, options);
    assert.equal(url.origin, 'https://sync.afraid.org');
    assert.equal(url.pathname, `/u/${fakeToken}/`);
    assert.equal(options.redirect, 'manual');
    const address = url.searchParams.get('ip');
    updates.push(address);
    if (rejectUpdate) return new Response('ERROR: invalid token');
    return new Response(`Updated wg.example.com from 2001:db8::1 to ${address}`);
  });

  await assert.rejects(syncEndpoint(config), /did not confirm/);
  rejectUpdate = false;
  const initial = await syncEndpoint(config);
  assert.equal(await syncEndpoint(config, initial), initial);
  port = 51821;
  const portChange = await syncEndpoint(config, initial);
  assert.equal(portChange, '2001::ca6d:cb00:7108');
  ipv4 = '203.0.113.9';
  const hostChange = await syncEndpoint(config, portChange);
  assert.equal(hostChange, '2001::ca6d:cb00:7109');
  assert.deepEqual(updates, [initial, initial, portChange, hostChange]);
});
