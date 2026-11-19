import { Resolver } from 'node:dns/promises';
import { isIPv4, isIPv6 } from 'node:net';
import { setTimeout as sleep } from 'node:timers/promises';
import { pathToFileURL } from 'node:url';

const resolver = new Resolver({ timeout: 3000, tries: 2 });
const requestTimeout = 8000;

function canonicalIPv6(address) {
  return new URL(`http://[${address}]/`).hostname.slice(1, -1);
}

export function encodeIp4p(ipv4, port) {
  if (!isIPv4(ipv4) || !Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error('IP4P requires an IPv4 address and a UDP port from 1 to 65535');
  }
  const [a, b, c, d] = ipv4.split('.').map(Number);
  const high = (a * 256 + b).toString(16);
  const low = (c * 256 + d).toString(16);
  return canonicalIPv6(`2001::${port.toString(16)}:${high}:${low}`);
}

export function udpEndpoint(payload) {
  if (!Array.isArray(payload?.urls)) {
    throw new Error('Pinggy /urls returned an unexpected response');
  }

  const matchEndpoint = /^(udp:\/\/)?([a-z0-9.-]+):([0-9]{1,5})\/?$/i;
  const candidates = payload.urls
    .filter((url) => typeof url === 'string')
    .map((url) => matchEndpoint.exec(url.trim()))
    .filter((match) => match && Number(match[3]) > 0 && Number(match[3]) <= 65535);

  const endpoint = candidates.find((match) => match[1]) ?? candidates[0];
  if (!endpoint) {
    throw new Error('Pinggy has no IPv4-capable UDP endpoint yet');
  }
  return { host: endpoint[2], port: Number(endpoint[3]) };
}

async function readResponse(url, service) {
  let response;
  try {
    response = await fetch(url, {
      signal: AbortSignal.timeout(requestTimeout),
      redirect: 'manual',
    });
  } catch {
    throw new Error(`${service} request failed or timed out`);
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new Error(`${service} returned HTTP ${response.status}`);
  }
  try {
    return await response.text();
  } catch {
    throw new Error(`${service} response could not be read`);
  }
}

async function pinggyPayload(url) {
  const response = await readResponse(url, 'Pinggy /urls');
  try {
    return JSON.parse(response);
  } catch {
    throw new Error('Pinggy /urls returned invalid JSON');
  }
}

async function resolveIPv4(host, port, previous) {
  if (isIPv4(host)) {
    return host;
  }

  let addresses;
  try {
    addresses = await resolver.resolve4(host);
  } catch {
    throw new Error(`Pinggy hostname ${host} has no reachable DNS A record`);
  }
  addresses = addresses.filter(isIPv4).sort();
  if (addresses.length === 0) {
    throw new Error(`Pinggy hostname ${host} has no DNS A record`);
  }
  return addresses.find((address) => encodeIp4p(address, port) === previous) ?? addresses[0];
}

async function updateFreeDNS(token, address) {
  const url = new URL(`https://sync.afraid.org/u/${encodeURIComponent(token)}/`);
  url.searchParams.set('ip', address);
  const body = (await readResponse(url, 'FreeDNS')).trim();

  const updated = /^Updated\s+\S+\s+from\s+\S+\s+to\s+(\S+)$/i.exec(body);
  if (updated && isIPv6(updated[1]) && canonicalIPv6(updated[1]) === address) {
    return;
  }
  if (/^No IP change detected\b/i.test(body)) {
    return;
  }
  throw new Error('FreeDNS did not confirm the AAAA update. Check the v2 token for the AAAA record');
}

export async function syncEndpoint({ pinggyUrl, freednsToken }, previous) {
  const payload = await pinggyPayload(pinggyUrl);
  const { host, port } = udpEndpoint(payload);
  const ipv4 = await resolveIPv4(host, port, previous);
  const address = encodeIp4p(ipv4, port);
  if (address !== previous) {
    await updateFreeDNS(freednsToken, address);
    console.log(`FreeDNS AAAA ${address} (${ipv4}:${port})`);
  }
  return address;
}

async function main() {
  const freednsToken = process.env.FREEDNS_TOKEN?.trim();
  if (!freednsToken || !/^[a-z0-9_-]+$/i.test(freednsToken)) {
    throw new Error('Set FREEDNS_TOKEN to the token from the FreeDNS v2 update URL');
  }
  const period = Number(process.env.POLL_INTERVAL_SECONDS ?? 10);
  if (!Number.isInteger(period) || period < 1 || period > 3600) {
    throw new Error('POLL_INTERVAL_SECONDS must be an integer from 1 to 3600');
  }

  const config = {
    pinggyUrl: new URL(process.env.PINGGY_URLS_URL ?? 'http://127.0.0.1:4300/urls'),
    freednsToken,
  };
  const maxBackoff = Math.max(period, 300);
  let lastPublished;
  let delay = period;
  console.log(`Polling Pinggy /urls every ${period}s`);
  for (;;) {
    try {
      lastPublished = await syncEndpoint(config, lastPublished);
      delay = period;
    } catch (error) {
      console.warn(`${error.message}. Retrying in ${delay}s`);
      await sleep(delay * 1000);
      delay = Math.min(delay * 2, maxBackoff);
      continue;
    }
    await sleep(delay * 1000);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
