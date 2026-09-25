# smokeping_prober

[![Build Status](https://github.com/SuperQ/smokeping_prober/actions/workflows/ci.yml/badge.svg)](https://github.com/SuperQ/smokeping_prober/actions/workflows/ci.yml)
[![Docker Repository on Quay](https://quay.io/repository/superq/smokeping-prober/status "Docker Repository on Quay")](https://quay.io/repository/superq/smokeping-prober)

Prometheus style "smokeping" prober.

![Example Graph](example-graph.png)

## Overview

This prober sends a series of ICMP (or UDP) pings to a target and records the responses in Prometheus histogram metrics.

```
usage: smokeping_prober [<flags>] [<hosts>...]

Flags:
  -h, --help                     Show context-sensitive help (also try --help-long and --help-man).
      --config.file=CONFIG.FILE  Optional smokeping_prober configuration yaml file.
      --web.telemetry-path="/metrics"
                                 Path under which to expose metrics.
      --web.systemd-socket       Use systemd socket activation listeners instead of port listeners (Linux only).
      --web.listen-address=:9374 ...
                                 Addresses on which to expose metrics and web interface. Repeatable for multiple
                                 addresses.
      --web.config.file=""       [EXPERIMENTAL] Path to configuration file that can enable TLS or authentication.
      --buckets="5e-05,0.0001,0.0002,0.0004,0.0008,0.0016,0.0032,0.0064,0.0128,0.0256,0.0512,0.1024,0.2048,0.4096,0.8192,1.6384,3.2768,6.5536,13.1072,26.2144"
                                 A comma delimited list of buckets to use
  -i, --ping.interval=1s         Ping interval duration
      --privileged               Run in privileged ICMP mode
  -s, --ping.size=56             Ping packet size in bytes
      --log.level=info           Only log messages with the given severity or above. One of: [debug, info, warn,
                                 error]
      --log.format=logfmt        Output format of log messages. One of: [logfmt, json]
      --version                  Show application version.

Args:
  [<hosts>]  List of hosts to ping
```

## Configuration

The prober can take a list of targets and parameters from the command line or from a yaml config file.

Example config:

```yaml
---
targets:
  - hosts:
      - host1
      - host2
    interval: 1s # Duration, Default 1s.
    network: ip # One of ip, ip4, ip6. Default: ip (automatic IPv4/IPv6)
    protocol: icmp # One of icmp, udp. Default: icmp (Requires privileged operation)
    size: 56 # Packet data size in bytes. Default 56 (Range: 24 - 65535)
    source: 127.0.1.1 # Souce IP address to use. Default: None (automatic selection)
```

In each host group the `interval`, `network`, and `protocol` are optional.

The interval Duration is in [Go time.ParseDuration()](https://golang.org/pkg/time/#ParseDuration) syntax.

The config is read on startup, and can be reloaded with the SIGHUP signal, or with an HTTP POST to the URI path `/-/reload`.

### Remote ping through a Huawei VRP router

A target group with `router:` is pinged by the router instead of the local host. The prober keeps persistent SSH sessions to the router and runs `ping -a <link source> <target>` once per link, so a router with several uplinks produces one series per uplink, told apart by the `link` label.

```yaml
routers:
  - name: ne8k
    address: 10.0.0.1:22
    username: smokeping
    password_file: /etc/smokeping_prober/ne8k.pass # or private_key_file
    known_hosts: /etc/smokeping_prober/known_hosts # or insecure_skip_host_key: true (lab only)
    sessions: 5 # Default 5
    links:
      - name: isp-a
        vpn_instance: upstream-1 # Optional, pings inside this VPN instance
        source: 192.0.2.1
        source6: 2001:db8::1 # Optional, needed for IPv6 targets
      - name: isp-b
        source: 198.51.100.1

targets:
  - host: 8.8.8.8
    router: ne8k
    interval: 1m # Default 1m for router targets
    count: 10 # Echo requests per run (-c). Default 10
    packet_interval: 50ms # Time between requests (-m). Default 50ms
    timeout: 500ms # Wait per reply (-t). Default 500ms
    # links: [isp-a]      # Optional subset of the router links
```

Notes:

- The router reports round-trip times in whole milliseconds, so remote histograms have 1 ms resolution.
- `protocol` is ignored and `source` cannot be set for router targets; the source comes from each link.
- Create `known_hosts` with `ssh-keyscan -p 22 10.0.0.1 > known_hosts` and check the fingerprint on the router.
- The SSH user only needs to run `ping` and `screen-length 0 temporary` in user view.
- A failed SSH session, timeout or unexpected output never counts as packet loss. It shows up in `smokeping_remote_errors_total` instead.
- `sessions` holds that many VTY lines on the router; keep it below the router's VTY limit (`user-interface maximum-vty`) so operators can still log in.
- The prober keeps its sessions busy, but if the VTY `idle-timeout` is short and targets are few, sessions may be dropped and reconnected; this shows in `smokeping_remote_errors_total{reason="connect"}`.
- Every ping is a CLI command: with HWTACACS command accounting or command logging enabled, expect one record per run.
- Encrypted (passphrase-protected) private keys are not supported.

## Building and running

Requires Go >= 1.22

```console
go install github.com/SuperQ/smokeping_prober@latest
sudo setcap cap_net_raw=+ep ${GOPATH}/bin/smokeping_prober
```

On multi-cpu systems it is typically more efficient to limit the prober to one CPU in order to
reduce the number of cross-cpu context switches and packet copies from the kernel to the prober.
This can be done with the `GOMAXPROCS` environment variable, or by using container (cgroup) limits.

```console
export GOMAXPROCS=1
./smokeping_prober <targets>
```

## Docker

```bash
docker run \
  -p 9374:9374 \
  --privileged \
  --env GOMAXPROCS=1 \
  quay.io/superq/smokeping-prober:latest \
  some-ping-target.example.com
```

## Metrics

| Metric Name                         | Type      | Description                                                                                  |
| ----------------------------------- | --------- | -------------------------------------------------------------------------------------------- |
| smokeping_requests_total            | Counter   | Counter of pings sent.                                                                       |
| smokeping_response_duration_seconds | Histogram | Ping response duration.                                                                      |
| smokeping_response_ttl              | Gauge     | The last response Time To Live (TTL).                                                        |
| smokeping_response_duplicates_total | Counter   | The number of duplicated response packets.                                                   |
| smokeping_receive_errors_total      | Counter   | The number of errors when Pinger attempts to receive packets.                                |
| smokeping_send_errors_total         | Counter   | The number of errors when Pinger attempts to send packets.                                   |
| smokeping_remote_sessions_up        | Gauge     | SSH sessions connected and ready per router.                                                 |
| smokeping_remote_errors_total       | Counter   | Remote ping runs with no result, by `reason` (`connect`, `timeout`, `parse`).                |
| smokeping_remote_jobs_skipped_total | Counter   | Remote runs dropped because the previous run for the same target and link was still pending. |

### TLS and basic authentication

The Smokeping Prober supports TLS and basic authentication.

To use TLS and/or basic authentication, you need to pass a configuration file
using the `--web.config.file` parameter. The format of the file is described
[in the exporter-toolkit repository](https://github.com/prometheus/exporter-toolkit/blob/master/docs/web-configuration.md).

### Health check

A health check can be requested in the URI path `/-/healthy`.
