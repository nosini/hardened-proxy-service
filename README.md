# hardened-proxy-service

A sandboxed systemd service for [mihomo](https://github.com/MetaCubeX/mihomo) in TUN mode,
with an nftables kill switch and an SELinux policy.

- `mihomo.service` runs mihomo as an unprivileged `mihomo` user in an empty, read-only root.
  Only `/usr`, the trust stores and `/etc/mihomo` are visible. Of those, only `ruleset/`,
  `cache.db` and `geoip.metadb` are writable, and nothing but mihomo itself, the unit's
  fail-closed `/usr/bin/false` and the loader and libc `/usr/bin/false` needs may be
  executed. `/etc/hosts` and `/etc/resolv.conf` are not mounted, so configure mihomo's DNS
  servers explicitly. In BPF-only mode, other users' processes are hidden; it learns which
  process owns a connection from socketowner.
- `killswitch.service` loads `nftables-killswitch.conf` before the network comes up. It
  drops anything that doesn't go through the TUN device, except mihomo's own sockets and
  the local addresses you allow in `/etc/nftables-killswitch.d/`. It filters forwarded
  traffic too, so bridged containers and VMs can't use the physical uplink directly. If
  mihomo stops, nothing leaks. If the rules fail to load, everything but loopback stays
  blocked. `killswitch-check.timer` loads them again if something flushes the table.
- `socketowner.service` attaches small BPF programs that record which process opened
  each connection, the program each process runs, and the owner's cgroup, for
  `PROCESS-NAME`, `PROCESS-PATH`, `FLATPAK-APP` and `SYSTEMD-UNIT` rules. It needs the
  kernel's BPF LSM; see [Process lookup](#process-lookup).
- `mihomo.te`, `mihomo.fc` and `mihomo.if` confine mihomo to `mihomo_t`, and
  socketowner to `socketowner_t`. The optional Wine observer has its own
  `socketowner_wine_t` domain.
- `mihomo-v6-direct.service` and its two scripts are optional. They add source-based routing
  rules so that mihomo's direct IPv6 connections from the uplink's own prefixes use the main
  routing table instead of looping back into the TUN. The rules follow prefix changes.
  With several uplinks, each one's prefixes use a copy of the main table without the other
  uplinks' default routes, so replies that arrive on a secondary uplink, such as a VLAN,
  pass a strict reverse-path filter like firewalld's `IPv6_rpfilter=strict`.

## Installing on openSUSE Tumbleweed

The [home:Nosini:proxy](https://build.opensuse.org/project/show/home:Nosini:proxy)
repository builds this service, socketowner and the
[mihomo build](https://github.com/nosini/mihomo) it expects from their `main` branches.
Its `mihomo` replaces openSUSE's, so the repository needs a higher priority than the
distribution's:

```sh
sudo zypper ar -p 90 -f https://download.opensuse.org/repositories/home:/Nosini:/proxy/openSUSE_Tumbleweed/ nosini-proxy
sudo zypper ref nosini-proxy
sudo zypper in hardened-proxy-service
```

`hardened-proxy-service` pulls in `mihomo`, the SELinux policy
(`hardened-proxy-service-selinux`) on systems with the targeted policy, and socketowner
as a recommended package. Two parts are optional packages:

- `hardened-proxy-service-wine-observer`: the
  [Wine observer](#wine-process-attribution) and its unit.
- `hardened-proxy-service-v6-direct`: the IPv6 direct routing, as
  `mihomo-v6-direct.service`.

The packages create the `mihomo` user, load the policy, create `/etc/mihomo` and install
the kill switch, the units, the generator and the modules-load file. Executables are in
`/usr/bin`, the units in `/usr/lib/systemd/system` and the IPv6 scripts in
`/usr/libexec/hardened-proxy-service`; the sections below show the paths of an
installation by hand, under `/usr/local` and `/etc/systemd/system`. No service is enabled
on installation. What remains is the configuration: write `/etc/mihomo/config.yaml` from
the example, fetch `geoip.metadb` if GEOIP rules are used, and list the LAN's addresses for
the kill switch, all as described in [Installing by hand](#installing-by-hand):

```sh
sudo install -m 640 -o root -g mihomo \
    /usr/share/doc/packages/hardened-proxy-service/config.example.yaml /etc/mihomo/config.yaml
```

Then enable the services. The kill switch blocks all traffic outside the tunnel as soon
as it loads, so check the configuration first on a remote machine:

```sh
sudo systemctl enable --now killswitch.service mihomo.service killswitch-check.timer
```

`sudo zypper up` installs new builds. An update restarts mihomo and reloads the kill
switch's rules in place, so the kill switch stays loaded throughout. A loader update takes
effect at `sudo systemctl restart socketowner.service`.

### Switching from an installation by hand

The files of an installation by hand take precedence over the packages': units and the
generator in `/etc/systemd`, and a policy module loaded with `semodule -i`, at priority 400
above the package's 200. Installing the packages also removes the enable links of units it
replaces, as openSUSE's presets leave new services disabled; the services keep running, but
wouldn't start at the next boot. After installing the packages, back up and remove the
files installed by hand, then enable the services again:

```sh
sudo tar czf /root/proxy-manual-backup.tar.gz --ignore-failed-read \
    /usr/local/bin/{mihomo,socketowner,socketowner-wine-observer} \
    /usr/local/sbin/mihomo-v6-{direct,watch}.sh \
    /etc/systemd/system/{mihomo,killswitch,killswitch-check,socketowner,socketowner-wine-observer,mihomo-v6-direct}.service \
    /etc/systemd/system/killswitch-check.timer \
    /etc/systemd/system-generators/mihomo-process-generator \
    /etc/{sysusers.d,tmpfiles.d,modules-load.d}/mihomo.conf
sudo rm -f /usr/local/bin/{mihomo,socketowner,socketowner-wine-observer} \
    /usr/local/sbin/mihomo-v6-{direct,watch}.sh \
    /etc/systemd/system/{mihomo,killswitch,killswitch-check,socketowner,socketowner-wine-observer,mihomo-v6-direct}.service \
    /etc/systemd/system/killswitch-check.timer \
    /etc/systemd/system-generators/mihomo-process-generator \
    /etc/{sysusers.d,tmpfiles.d,modules-load.d}/mihomo.conf
sudo semodule -X 400 -r mihomo
sudo restorecon -Rv /etc/mihomo
sudo systemctl daemon-reload
sudo systemctl reenable killswitch.service mihomo.service killswitch-check.timer
sudo systemctl reload killswitch.service
sudo systemctl try-restart socketowner.service socketowner-wine-observer.service
sudo systemctl restart mihomo.service
```

Add `mihomo-v6-direct.service` to the `reenable` line if it was enabled. A kill switch
config that differed from the package's is kept as `/etc/nftables-killswitch.conf.rpmorig`;
compare the two before the reload. `systemctl cat mihomo.service` then shows
`ExecStart=/usr/bin/mihomo -d /etc/mihomo` in the generated drop-in, and `ps -eZ | grep
mihomo_t` the running service.

## Installing by hand

Install the mihomo binary as `/usr/local/bin/mihomo`.
Install Python 3 and PyYAML from your distribution. The process-lookup generator uses
them to read `/etc/mihomo/config.yaml`; `python3 -I -c 'import yaml'` checks availability.

Create the user, then the SELinux module, so that the directories created next get the
right labels:

```sh
sudo install -m 644 mihomo.sysusers /etc/sysusers.d/mihomo.conf
sudo systemd-sysusers /etc/sysusers.d/mihomo.conf

make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo restorecon -v /usr/local/bin/mihomo

sudo install -m 644 mihomo.tmpfiles /etc/tmpfiles.d/mihomo.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/mihomo.conf

sudo install -m 644 mihomo.modules-load /etc/modules-load.d/mihomo.conf
sudo systemctl restart systemd-modules-load.service
```

The policy labels both `/usr/local/bin` and `/usr/bin`, since `/usr/local` isn't an
equivalence of `/usr` on every distribution. Check the label and, after starting mihomo,
its domain:

```sh
ls -Z /usr/local/bin/mihomo
ps -eZ | grep mihomo
```

The first shows `mihomo_exec_t`, the second `mihomo_t`. Without the module's label, mihomo
runs unconfined. The same applies to `socketowner` and `socketowner-wine-observer` when
you install them.

Write the config. `config.example.yaml` matches the unit and the kill switch:

```sh
sudo install -m 640 -o root -g mihomo config.example.yaml /etc/mihomo/config.yaml
```

mihomo normally downloads `geoip.metadb` the first time a GEOIP rule is used. It can't do
that here, since `/etc/mihomo` is read-only to it. If you use GEOIP rules, fetch the file
once; after that, mihomo can update it in place:

```sh
sudo curl -fLo /etc/mihomo/geoip.metadb \
    https://github.com/MetaCubeX/meta-rules-dat/releases/download/latest/geoip.metadb
sudo chown mihomo:mihomo /etc/mihomo/geoip.metadb
sudo chmod 600 /etc/mihomo/geoip.metadb
sudo restorecon -v /etc/mihomo/geoip.metadb
```

Put your LAN and any other addresses apps should reach directly in a file under
`/etc/nftables-killswitch.d/`. The kill switch includes every `*.conf` file there:

```sh
sudo mkdir -p /etc/nftables-killswitch.d
echo 'add element inet killswitch direct4 { 192.168.1.0/24 }' |
    sudo tee /etc/nftables-killswitch.d/lan.conf
```

IPv6 addresses go in `direct6` the same way, for example
`add element inet killswitch direct6 { fd00:1::/64 }`.

The `forward` chain filters traffic the host forwards, such as containers and VMs on a
bridge: only established flows, the TUN and `direct4`/`direct6` pass. VMs attached with
macvlan or macvtap send through the uplink without passing the host's forward hook, so the
kill switch can't see them; filter inside the VM or don't give it an uplink. With
`br_netfilter` loaded (`bridge-nf-call-iptables` = 1), traffic bridged between the ports of
a bridge does pass this hook and is dropped. If VMs on such a bridge should reach the
network directly, allow the bridge in a file under `/etc/nftables-killswitch.d/`:

```sh
echo 'add rule inet killswitch forward iifname "br0" oifname "br0" accept' |
    sudo tee /etc/nftables-killswitch.d/bridge.conf
```

Only frames that stay on that bridge match; routing from it to the uplink stays blocked. On IPv6 the
output and input chains allow only what the uplink needs: neighbor discovery, router
solicitation, MLD and DHCPv6 to `ff02::1:2` or a link-local server. mDNS isn't allowed.
DHCPv4 is limited to broadcasts and the LAN addresses in `direct4`.

Then install the kill switch, units and generator:

```sh
sudo install -m 644 nftables-killswitch.conf /etc/nftables-killswitch.conf
sudo nft -c -f /etc/nftables-killswitch.conf
sudo install -m 644 killswitch.service mihomo.service /etc/systemd/system/
sudo install -D -m 755 mihomo-process-generator \
    /etc/systemd/system-generators/mihomo-process-generator
sudo restorecon -v /etc/systemd/system-generators/mihomo-process-generator
sudo systemctl daemon-reload
```

The generator goes in `/etc`, not `/usr/local`: systemd runs generators at boot before it
mounts file systems other than `/` and `/usr`, and `/usr/local` is often one of them (a
btrfs subvolume on openSUSE). Without the generator, mihomo doesn't start.

The generator also adds `CAP_NET_BIND_SERVICE` when a checked listener port is below
1024: `dns.listen` when `dns.enable` is true, `mixed-port`, `port`, `socks-port`,
`redir-port`, `tproxy-port`, `external-controller`, `external-controller-tls` (both
`host:port`, including `[::]:53`), `tuic-server.listen` when `tuic-server.enable` is
true, and `listeners[].port` (single ports, comma lists and ranges). Higher ports run
without it. `mihomo.te` allows `name_bind` only on `dns_port_t` and `unreserved_port_t`,
so other ports below 1024, such as 80 or 443, still need an SELinux policy allowance
even with `CAP_NET_BIND_SERVICE`.

`external-controller` serves an API that can switch mihomo to direct mode, and the kill
switch lets everything mihomo's UID sends out, so an unauthenticated controller is a kill
switch bypass: any local process that reaches it can route traffic around the tunnel. Set
`secret` in `config.yaml` to a long random value (`openssl rand -hex 32`) and send it in
the `Authorization: Bearer` header, or leave the controller off.

The modules-load file loads `tun` and the nftables modules at boot. Restarting
`systemd-modules-load.service` loads them now, before mihomo starts; see
[Kernel modules and TPROXY](#kernel-modules-and-tproxy).

The example uses BPF-only process lookup. Before starting mihomo, install
socketowner as described in [Process lookup](#process-lookup), or set
`find-process-mode: off` in `config.yaml` if you don't use process rules. Then start and
enable the services:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now killswitch.service mihomo.service
```

The kill switch blocks all traffic outside the tunnel as soon as it loads. Make sure mihomo
connects before enabling it on a remote machine.

After changing a file in `/etc/nftables-killswitch.d/`, swap the rules with
`sudo systemctl reload killswitch.service`. If the rules don't load, at boot or on a
reload, the previous table stays in place. At boot that's a table that blocks everything
but loopback, so the machine stays offline. `sudo journalctl -u killswitch.service` shows
the error.

A reload of `nftables.service`, whose config usually begins with `flush ruleset`, or a
manual `nft flush ruleset` deletes the table while `killswitch.service` still reports
active. The timer checks for it and loads the rules again:

```sh
sudo install -m 644 killswitch-check.service killswitch-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now killswitch-check.timer
```

It checks every 10 s, skips the check while `killswitch.service` is stopped, and writes
`killswitch-check: table inet killswitch is missing, loading it again` to the journal
(`journalctl -u killswitch-check.service`) before loading the rules again. To get online
without the kill switch, remove its table and stop the timer that would put it back:

```sh
sudo systemctl stop killswitch-check.timer
sudo nft delete table inet killswitch
```

`sudo systemctl stop killswitch.service` also removes the table, and the timer leaves it
alone until the service starts again.

For the IPv6 direct routing:

```sh
sudo install -m 755 mihomo-v6-direct.sh mihomo-v6-watch.sh /usr/local/sbin/
sudo install -m 644 mihomo-v6-direct.service /etc/systemd/system/
sudo systemctl enable --now mihomo-v6-direct.service
```

The uplinks are the interfaces that hold a default route. To fix them instead, add the
interface names to `ExecStart=` in `mihomo-v6-direct.service`. Each uplink gets a routing
table, numbered 8999000 plus its interface index, holding that copy. The rules come at
priorities 8998 and 8999, ahead of mihomo's at 9000.

The unit keeps only `CAP_NET_ADMIN`, the capability `ip rule` and `ip route` need: `/usr`,
`/etc`, `/sys` and `/proc/sys` are read-only, `/dev` and `/tmp` are private, only netlink and
`AF_UNIX` (journald) sockets can be opened, and the syscall filter is `@system-service`.

## Process lookup

`find-process-mode: always` lets rules match the process behind a connection
(`PROCESS-NAME`, `PROCESS-PATH`) and its cgroup (`FLATPAK-APP`, `SYSTEMD-UNIT`).
`mihomo-process-generator` reads the lookup settings
from `/etc/mihomo/config.yaml` at boot and on `systemctl daemon-reload`, and generates a
drop-in for `mihomo.service`:

| YAML settings | Service settings |
| --- | --- |
| `find-process-mode: off` | Other users' processes hidden; no BPF maps or inspection capabilities |
| `find-process-bpf-only: true` with matching enabled | Other users' processes hidden; requires socketowner and its maps |
| `find-process-bpf-only` absent with matching enabled | With `find-process-bpf-map: /run/mihomo-bpf/conn_owners`, the BPF-only sandbox (with a warning to set `find-process-bpf-only: true`); otherwise as `off`, so lookups fail. `/proc` is never granted |
| `find-process-bpf-only: false` with matching enabled | Processes visible; adds `CAP_SYS_PTRACE` and `CAP_DAC_READ_SEARCH`; with `find-process-bpf-map: /run/mihomo-bpf/conn_owners`, also starts socketowner and binds its maps |

Matching is enabled with `always` or `strict` (mihomo's default). Only an explicit
`find-process-bpf-only: false` selects the `/proc` fallback. Mihomo then tries BPF first:
the owner and executable path if recorded, otherwise the BPF owner's `/proc/<pid>/exe`. If
that fails or BPF has no owner, it falls back to netlink and a `/proc` search. Leaving the
key out uses the BPF-only sandbox when `find-process-bpf-map:
/run/mihomo-bpf/conn_owners` is set, and logs a warning because mihomo itself
defaults to allowing `/proc` fallback, which hits denials inside that sandbox.
Without that map path, as in an upstream config,
mihomo runs with no lookup privileges and its process rules don't match.

An omitted `find-process-bpf-map` uses mihomo's default map path; an explicitly empty
value disables BPF lookup. The base unit denies `bpf()`, and the generator allows it
only in the BPF-only and `/proc` modes; with lookup off mihomo cannot make BPF calls.
In `/proc` mode the generator adds the BPF parts, socketowner, the `bpf()` permission
and the map mount, only when the map path is `/run/mihomo-bpf/conn_owners`. With any
other path it warns that BPF lookup, `FLATPAK-APP`, `SYSTEMD-UNIT` and
`WINE-PROCESS-*` rules then need `find-process-bpf-map:
/run/mihomo-bpf/conn_owners`. Mihomo itself never needs `CAP_BPF` to read the maps.

The service keeps a read-only `/proc` for its own memory statistics and other runtime
uses. `ProtectProc=invisible` hides other users' processes in BPF-only and off modes.
The generator does not inspect whether the loader is currently active. When the map
path is the service's, both lookup modes start it with mihomo. In BPF-only mode,
attachment failure prevents mihomo from starting; in `/proc` mode, mihomo starts
anyway and falls back to `/proc`.

After changing lookup mode, reload and restart; an API configuration reload alone cannot
change an existing process's sandbox:

```sh
sudo systemctl daemon-reload
sudo systemctl restart mihomo.service
```

`sudo systemctl cat mihomo.service` shows the generated settings. If the generator is
missing, Python/PyYAML is unavailable, or the lookup settings cannot be read, mihomo
fails to start. The generator supports plaintext YAML, including anchors and merges;
BPF-only mode requires `find-process-bpf-map: /run/mihomo-bpf/conn_owners`.

### With socketowner

`socketowner.service` attaches BPF programs that record the process behind each TCP
and UDP connection and the program each process runs. mihomo reads both from tables that
`mihomo.service` binds into its sandbox read-only; process lookup never reads `/proc`.
The tables keep an entry after its connection closes and after its process exits, so a short UDP
exchange still matches its rule. Looking a connection up takes microseconds. When a
process passes a socket to another, the tables name the process that used it.

This needs the BPF LSM, which most distribution kernels build in but not all turn on.
Check that `bpf` is in the list of active LSMs:

```sh
cat /sys/kernel/security/lsm
```

If it's missing, look processes up [through /proc](#through-proc) instead. This setup has
been tested on Linux 7.3.

The loader needs `CAP_BPF`, `CAP_PERFMON`, `CAP_NET_ADMIN` and `CAP_CHOWN`, and only while
it attaches the programs; it exits afterwards and the programs stay attached.
`CAP_PERFMON` lets BPF programs read kernel memory, which recording what each process runs
requires. The programs are built into the loader and checked by the kernel when they're
loaded, and the loader has no network access and takes no input but its command line. In
return, mihomo, which handles network traffic and serves its API, needs no capabilities
and no access to other processes for this.

Install `socketowner` separately from the
[socketowner repository](https://github.com/nosini/socketowner). It is no longer
part of the mihomo source or build. To build it with Go 1.25 or newer, run these
commands from a socketowner checkout:

```sh
CGO_ENABLED=0 go build -o socketowner .
sudo install -m 755 socketowner /usr/local/bin/socketowner
sudo restorecon -v /usr/local/bin/socketowner
```

The loader and mihomo use [map interface v1](https://github.com/nosini/socketowner/blob/main/docs/map-interface.md).
The updated mihomo reader requires the loader's `abi_version` map. When migrating
from the loader formerly included in mihomo, install this loader and restart it
before starting the updated mihomo:

```sh
sudo systemctl restart socketowner.service
```

Keep loader installation and updates separate from mihomo updates; replacing
mihomo must not replace the privileged executable.

Then install the unit and restart mihomo. The generator starts the loader with mihomo in
BPF-only mode, and in `/proc` mode only when `find-process-bpf-map` is the
service's `/run/mihomo-bpf/conn_owners`, so it needs no enabling:

```sh
sudo install -m 644 socketowner.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart mihomo.service
```

In BPF-only mode, mihomo requires it. Otherwise process rules would silently stop matching
whenever the programs aren't attached. If they fail to attach, mihomo doesn't start, and
`sudo journalctl -u socketowner.service` shows why. Restarting or stopping
socketowner restarts or stops mihomo too. In `/proc` mode, mihomo starts even if the
loader fails, and searches `/proc` instead.

`config.example.yaml` already points `find-process-bpf-map` at the tables and sets
`find-process-bpf-only: true`. When mihomo opens them, `sudo journalctl -u mihomo.service`
shows "Using socket owners and executables recorded in /run/mihomo-bpf".

Some processes can't be named:

- Connections opened before socketowner started that have not sent since it
  attached. Processes that were already running are filled in when it starts,
  and with `-exec-paths` a socket is attributed to its sender on the first send
  after attach; without `-exec-paths`, earlier connections remain unnamed.
- Programs whose path is longer than 1024 bytes.
- A process in a plain `chroot` is named by its path inside the chroot. Containers and
  sandboxes such as Flatpak show their in-sandbox path, as they do in `/proc`.

### Through /proc

Without the BPF LSM, mihomo can find the process by reading the `fd` and `exe` links under
`/proc/<pid>/` of other users' processes. That takes `CAP_SYS_PTRACE` and
`CAP_DAC_READ_SEARCH` and only works while the connection is still open. The SELinux policy
keeps mihomo from opening those processes' files, so it can't read their environment,
command line or memory. It can't stop mihomo from following their `root` and `cwd` links,
though, which work the same way as `exe`. Through them a compromised mihomo can reach files
outside its sandbox, as far as `mihomo_t` may read them.

To use it, set `find-process-bpf-only: false` in `config.yaml` and enable the SELinux
boolean, which the policy leaves off by default:

```sh
sudo setsebool -P mihomo_find_process_proc on
sudo systemctl daemon-reload
sudo systemctl restart mihomo.service
```

The generator adds the two capabilities and makes the process entries visible. It can't set
booleans: while `mihomo_find_process_proc` is off it logs a warning at boot with the
`setsebool` command, and `/proc` lookup then fails with AVC denials.

In `/proc` mode the generator starts `socketowner.service`, and the Wine observer when
installed, only when `find-process-bpf-map: /run/mihomo-bpf/conn_owners` is set. Without
the BPF LSM socketowner can still record which process owns each connection, though not
what each process runs. That keeps short UDP exchanges matching their rules while the
process runs; mihomo then reads the path from `/proc`. Without it, though, a connected
socket that one process hands to another is still credited to the first.

Without `-exec-paths` there is a second cost. mihomo must still check each BPF-recorded
owner's PID start time through `/proc/<pid>/stat`, because a PID can be reused, and the
policy deliberately grants no `open` on other domains' files — that is what keeps their
`environ`, `cmdline` and `mem` closed. Under enforcing SELinux every such lookup is
denied: it logs an AVC denial for the `open` and the start-time check is skipped, with
the executable still read from the `/proc/<pid>/exe` link, which the policy allows.
Running socketowner with `-exec-paths` records executables in the maps and avoids both
the check and the denials, but it needs the BPF LSM. Install it as
above and run it without `-exec-paths`:

```sh
sudo mkdir -p /etc/systemd/system/socketowner.service.d
printf '%s\n' '[Service]' 'ExecStart=' \
    'ExecStart=/usr/local/bin/socketowner -reader-group mihomo attach' \
    'CapabilityBoundingSet=' \
    'CapabilityBoundingSet=CAP_BPF CAP_NET_ADMIN CAP_CHOWN' |
    sudo tee /etc/systemd/system/socketowner.service.d/no-exec-paths.conf
sudo systemctl daemon-reload
sudo systemctl restart socketowner.service
sudo systemctl restart mihomo.service
```

If `socketowner.service` fails at boot before it creates `/sys/fs/bpf/mihomo/maps`,
`mihomo.service` starts without `/run/mihomo-bpf` mounted. Starting `socketowner.service`
later then requires restarting `mihomo.service` too, so the bind mount appears in its
sandbox.

### Turning it off

If you don't use these rules, set `find-process-mode: off` in `config.yaml`. mihomo then
no longer starts socketowner or the Wine observer. Reload first so stopping the loader
doesn't stop mihomo:

```sh
sudo systemctl daemon-reload
sudo systemctl stop socketowner.service
sudo systemctl restart mihomo.service
```

### Wine process attribution

The optional `socketowner-wine-observer.service` runs continuously and discovers
wineservers and their sandbox pastas. It lets a compatible mihomo build match Windows
executables by name or path with `WINE-PROCESS-NAME`, `WINE-PROCESS-PATH`, and their
`-REGEX` and `-WILDCARD` variants. Native process lookup continues through
`socketowner.service`. The observer and reader are available on the
`main` branches of socketowner and mihomo. See socketowner's
[Wine attribution documentation](https://github.com/nosini/socketowner/tree/main/wine)
for capture limits, unknown flows and DNS handling.

From the socketowner checkout, build and install the observer:

```sh
CGO_ENABLED=0 go build -o socketowner-wine-observer ./wine/cmd/socketowner-wine-observer
sudo install -m 755 socketowner-wine-observer /usr/local/bin/socketowner-wine-observer
```

Then, from this checkout, update the policy and install the unit:

```sh
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo restorecon -v /usr/local/bin/socketowner-wine-observer
sudo install -m 644 socketowner-wine-observer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart mihomo.service
```

The observer has no `[Install]` section and needs no `systemctl enable`: the
generator makes `mihomo.service` want it only where it also starts the loader
(BPF-only mode, or `/proc` mode with `find-process-bpf-map:
/run/mihomo-bpf/conn_owners`) and the unit is installed and not masked.
The observer starts after the native loader and before mihomo, and signals readiness only
after its hooks and maps are ready. Start it before launching games. It restarts with the
native loader; the loader's detach leaves the observer's pins alone, and the observer
re-pins any that disappear. Following a crash, it waits six seconds before restarting so
it can replace the old heartbeat and pins.

The observer runs as root in `socketowner_wine_t`, separate from the oneshot
loader's domain. Its capability set includes `CAP_SYS_PTRACE` and
`CAP_DAC_READ_SEARCH` to discover pasta through other users' process fd links,
as well as `CAP_BPF`, `CAP_PERFMON`, `CAP_NET_ADMIN` and `CAP_CHOWN`. It sees the
host network namespace and process links, while its filesystem view is an empty,
read-only root with only the paths it uses bound in. SELinux grants read-only
process inspection. The only other processes' files it opens are pasta command
lines; the policy allows that open for processes in `pasta_t` and
`sandbox_pasta_t`. No process tracing or memory access is granted. Wine,
pasta and games stay unprivileged. Published maps remain root-owned, with read
access for the mihomo group.

When the observer registers a pasta, it reads that pasta's command line, for
the `--dns-forward` and `--dns-host` addresses that decide whether a DNS query
joins its client or stays `wine-unknown`. The command line is the only source:
pasta takes its default `--dns-host` from a resolv.conf that its own isolation
hides afterwards, so a sandbox that wants its DNS joined starts pasta with
`--dns-host`. The policy opens one proc file for two domains: package pasta
runs in passt's `pasta_t`, and the copy run-in-sandbox's `sandbox-game` runs
from `/run/user/<uid>` runs in `sandbox_pasta_t`, from run-in-sandbox's own
SELinux module (`sudo ./install.sh --selinux` in its checkout). Without that
module the copy runs unconfined, and its command line stays closed to the
observer. Pasta running in `container_runtime_t`, for example under Podman or Quadlet,
is likewise outside both domains: `socketowner_wine_t` can't open its command line, so
its DNS flows stay `wine-unknown` while `/process-lookup` still reports healthy. Each
domain is required inside its own `optional_policy`, so this module also loads where
either module isn't installed; there the read fails, the observer reports it, and that
pasta's DNS flows stay unknown.

Check its state and recent discoveries with:

```sh
sudo systemctl status socketowner-wine-observer.service
sudo journalctl -u socketowner-wine-observer.service -n 30
```

Stopping the observer removes its Wine maps on a clean exit; a crash leaves stale
records until restart. mihomo treats stale records as `wine-unknown`. The
process remains the sandbox's pasta, so a pasta rule after the Wine rules is the
fallback for unknown flows and those without a Wine record. Decide DNS exceptions
before adding a blanket `wine-unknown` rejection. Stopping this optional service leaves
native process lookup available. The generator starts it again with mihomo wherever it
starts the loader and the unit is installed and not masked; remove its unit file to keep
it off:

```sh
sudo systemctl stop socketowner-wine-observer.service
sudo rm /etc/systemd/system/socketowner-wine-observer.service
sudo systemctl daemon-reload
```

A unit installed under `/usr/lib/systemd/system`, as a package installs it, is not
removed that way; `sudo systemctl mask socketowner-wine-observer.service` keeps it off
without touching the file.

### Routing by Flatpak app

Flatpak apps often can't be told apart by process: JDownloader, for example, runs as
`java` from the same bundled runtime path as other Java apps. `FLATPAK-APP` matches the
app ID instead:

```yaml
rules:
  - FLATPAK-APP,org.jdownloader.JDownloader,proxy
```

When a desktop session starts an app with `flatpak run`, Flatpak puts it in a systemd
scope named after its app ID (`app-flatpak-<app ID>-<instance>.scope`). socketowner
records the cgroup of each connection's owner and, with `-exec-paths`, the cgroup's
path; mihomo takes the app ID from the nearest Flatpak scope in that path. A scope that
has ended by the time the connection is looked up is still named. App IDs are
compared exactly, including case; `flatpak list --app` shows them. The connection list
of the external controller shows `flatpakApp` and `cgroupPath` for such connections.

This needs a socketowner build that publishes owner cgroups and a mihomo build with the
rule. With an older loader the rule never matches, mihomo logs one warning, and other
process rules keep working. An app started without a systemd user session, so without
its scope, isn't recognised either.

In BPF-only mode mihomo needs nothing more. In `/proc` mode the loader may run without
`-exec-paths` and so record no paths; mihomo then lists `/sys/fs/cgroup` instead, which
`ProtectControlGroups=` mounts read-only in its sandbox. The SELinux policy lets mihomo
list its directories but not open the files in them.

Like process names, scope names are chosen by the user's own processes: any process can
create a scope with any name through its systemd user manager
(`systemd-run --user --scope --unit=app-flatpak-…`) or move itself between its own
cgroups. `FLATPAK-APP` routes cooperative apps; it is not a security boundary, and a
`REJECT` rule based on it can be evaded by the app it names.

### Routing by systemd unit

`SYSTEMD-UNIT` matches the systemd service or scope whose cgroup holds the connection's
owner:

```yaml
rules:
  - SYSTEMD-UNIT,jdownloader.service,proxy
  - SYSTEMD-UNIT,backup.service,direct
```

The unit is the nearest service or scope in the owner's cgroup path; slices are skipped,
and cgroups below a unit belong to it. Unit names are compared exactly, including case
and systemd escapes such as `\x2d`. The connection list of the external controller shows
`systemdUnit` for such connections.

This uses the same socketowner cgroup records as `FLATPAK-APP`, so it needs a loader that
publishes owner cgroups and a mihomo build with the rule. In BPF-only mode mihomo needs
nothing more; in `/proc` mode it may list `/sys/fs/cgroup` as described above. Like
`FLATPAK-APP`, `SYSTEMD-UNIT` routes cooperative units, not a security boundary: any
process can start a scope with any name through its systemd user manager or move itself
between its own cgroups, and a `REJECT` rule based on it can be evaded by the process it
names.

### Checking lookup health

The mihomo build serves `GET /process-lookup` on its external controller: whether
the socketowner maps are open and valid, with any error from opening or
validating them; whether `find-process-bpf-only` is active; the Wine observer's
heartbeat age and whether it counts as stale; and counters since start for BPF
hits and misses, `/proc` fallbacks and Wine records found known or unknown.
Its `cgroups` part includes `available`, which says whether connections can be
attributed to cgroups for `FLATPAK-APP` and `SYSTEMD-UNIT` rules, where their names
come from (`source`: the loader's `bpf` paths or a `cgroupfs` listing), and why not: a
loader that doesn't publish owner cgroups (`mapError`), or a `/sys/fs/cgroup` mihomo
can't list (`rootError`).
Wine problems are also logged once when the heartbeat goes stale or the maps
disappear, and once when they return. See mihomo's
[lookup health documentation](https://github.com/nosini/mihomo/blob/main/docs/socketowner.md#lookup-health)
for the response and the exact messages.

`config.example.yaml` leaves the controller off. To check locally, enable one
in `/etc/mihomo/config.yaml`, on a port SELinux labels `unreserved_port_t` as
the example comments note, and restart mihomo:

```yaml
external-controller: 127.0.0.1:9097
secret: "replace-with-openssl-rand-hex-32"
```

```sh
curl -s -H "Authorization: Bearer <secret>" http://127.0.0.1:9097/process-lookup |
    python3 -m json.tool
```

The controller can also switch mihomo to direct mode, and the kill switch lets
everything mihomo's UID sends out, so keep the `secret` set as the example
comments describe. mihomo also has `external-controller-unix`, but the unit's
SELinux policy grants mihomo no Unix listening sockets, so use the TCP listener
here.

## Kernel modules and TPROXY

mihomo uses the `tun` module and nftables expressions. The `mihomo.modules-load` file loads
`tun` and the nftables modules that TUN `auto-redirect` needs at boot, so mihomo doesn't have
to ask the kernel for them. A missing nftables module shows up as `Start TUN listening error:
auto redirect: ... netlink receive: no such file or directory`. If mihomo logs a failure to
load a module or an AVC for `module_request`, add that module's name to
`/etc/modules-load.d/mihomo.conf` and restart `systemd-modules-load.service`, then mihomo.
If you can't load it that way, enable `mihomo_load_modules`:

```sh
sudo setsebool -P mihomo_load_modules on
```

TPROXY, with a nonzero `tproxy-port`, makes mihomo run `iptables` to set up its redirect
rules when `iptables.enable: true` and TUN is off (`tun.enable` is not true). The policy
grants that only with `mihomo_use_iptables`, which is off by default:

```sh
sudo setsebool -P mihomo_use_iptables on
```

The generator warns at boot when mihomo will run `iptables` and the boolean is off. Only
then does its drop-in give the unit what `iptables` and `ip` need: it lifts the
`NoExecPaths=` restriction, binds `/lib64` (their ELF loader) and `/etc/alternatives` (where
Debian-style systems link `iptables`) read-only, and adds `CAP_NET_RAW` for the raw socket of
iptables' legacy backend, the default on openSUSE. The legacy backend also takes the xtables
lock, which the drop-in points at `/run/mihomo/xtables.lock` because the service can't write
the host's `/run/xtables.lock`; mihomo's rule setup therefore doesn't wait for other
legacy `iptables` commands running at the same moment. SELinux still allows exec only of
those commands, the lock file and loading the xtables modules they use, and only with the
boolean. The service keeps `/proc/sys` read-only, so with an `inbound-interface` other than
`lo` the host must enable `net.ipv4.ip_forward` itself.

## Updating

With the packages, `sudo zypper up` installs the new files, policy and units; settings in
`config.yaml` and SELinux booleans that a version note asks for still have to be changed
by hand. The commands below are for installations by hand.

Version 1.13 adds `FLATPAK-APP` rules, and `SYSTEMD-UNIT` rules use the same cgroup
records. BPF-only mode takes cgroup names from socketowner; for a loader without
`-exec-paths`, the policy lets mihomo list `/sys/fs/cgroup` instead.
Install the updated socketowner (see [With socketowner](#with-socketowner)) and mihomo
builds, load the policy and restart both services:

```sh
sudo install -m 755 socketowner /usr/local/bin/socketowner
sudo install -m 755 mihomo /usr/local/bin/mihomo
sudo restorecon -v /usr/local/bin/socketowner /usr/local/bin/mihomo
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo systemctl restart socketowner.service
sudo systemctl restart mihomo.service
```

Version 1.11 confines `/proc` lookup and `iptables` behind SELinux booleans, stops mihomo
from loading kernel modules itself, grants `CAP_NET_BIND_SERVICE` from the generator instead
of the unit, and filters forwarded traffic. On upgrade, install and load the modules-load file, the
policy, the unit and the generator, then reload the kill switch and restart mihomo.
Users in `/proc` lookup mode must also enable `mihomo_find_process_proc`; TPROXY users must
enable `mihomo_use_iptables`. A config without `find-process-bpf-only` no longer gets `/proc`
lookup: add `find-process-bpf-map: /run/mihomo-bpf/conn_owners`, as in
`config.example.yaml`, for BPF lookup, or set `find-process-bpf-only: false` to keep `/proc`
lookup.

```sh
sudo install -m 644 mihomo.modules-load /etc/modules-load.d/mihomo.conf
sudo systemctl restart systemd-modules-load.service
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo setsebool -P mihomo_find_process_proc on   # only for /proc lookup
sudo setsebool -P mihomo_use_iptables on        # only for TPROXY
sudo install -m 644 nftables-killswitch.conf /etc/nftables-killswitch.conf
sudo install -m 644 mihomo.service /etc/systemd/system/
sudo install -D -m 755 mihomo-process-generator \
    /etc/systemd/system-generators/mihomo-process-generator
sudo systemctl daemon-reload
sudo systemctl reload killswitch.service
sudo systemctl restart mihomo.service
```

Version 1.9 adds the optional Wine observer's domain and unit. For Wine attribution,
follow [Wine process attribution](#wine-process-attribution); native process lookup
needs no additional unit.

Version 1.8 renames the loader from `mihomo-sockowner` to `socketowner`, along with its
unit and SELinux types. Stop mihomo and the old loader, remove both old files, then build
and install the renamed loader from a socketowner checkout. The kill switch keeps
blocking traffic while mihomo is stopped:

```sh
sudo systemctl stop mihomo.service mihomo-sockowner.service
sudo rm /etc/systemd/system/mihomo-sockowner.service /usr/local/bin/mihomo-sockowner
CGO_ENABLED=0 go build -o socketowner .
sudo install -m 755 socketowner /usr/local/bin/socketowner
```

Then, from this repository, load the policy, label the new binary and install the units:

```sh
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo restorecon -v /usr/local/bin/socketowner
sudo install -m 644 mihomo.service socketowner.service /etc/systemd/system/
sudo install -D -m 755 mihomo-process-generator \
    /etc/systemd/system-generators/mihomo-process-generator
sudo systemctl daemon-reload
sudo systemctl start mihomo.service
```

If you created the `no-exec-paths.conf` loader drop-in, remove
`/etc/systemd/system/mihomo-sockowner.service.d` and create it again as described in
[Through /proc](#through-proc), before reloading.

When updating from the manually configured lookup sandbox, install the generator and
both units. Remove the old capability drop-in and the loader's old enablement link, so
that lookup settings in `config.yaml` control the sandbox and dependency. Version 1.7
also labels the generator in `/etc/systemd/system-generators`, lets it read the
configuration under SELinux, and allows both
lookup methods unconditionally. The old `mihomo_find_process` and
`mihomo_find_process_proc` booleans are removed; YAML and the generated systemd sandbox
select the available lookup methods:

```sh
sudo rm -f /etc/systemd/system/mihomo.service.d/proc-lookup.conf
sudo systemctl disable mihomo-sockowner.service
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo install -m 644 mihomo.service socketowner.service /etc/systemd/system/
sudo install -D -m 755 mihomo-process-generator \
    /etc/systemd/system-generators/mihomo-process-generator
sudo restorecon -v /etc/systemd/system-generators/mihomo-process-generator
sudo systemctl daemon-reload
sudo systemctl restart mihomo.service
```

socketowner no longer needs to be enabled: the generator starts it with mihomo
in BPF-only mode, and in `/proc` mode when `find-process-bpf-map` is the
service's `/run/mihomo-bpf/conn_owners`. In BPF-only mode, remove any
`no-exec-paths.conf` loader drop-in so it records executables too.

Rebuild and load the SELinux module, relabel `/etc/mihomo` (version 1.3 gave mihomo's
writable files a type of their own) and, if you use it, `/usr/local/bin/socketowner`
(version 1.5 added its type). Then install the files again as above and reload:

```sh
make -f /usr/share/selinux/devel/Makefile mihomo.pp
sudo semodule -i mihomo.pp
sudo restorecon -Rv /etc/mihomo
sudo restorecon -v /usr/local/bin/socketowner
sudo systemctl daemon-reload
sudo systemctl reload killswitch.service
sudo systemctl restart mihomo.service mihomo-v6-direct.service
```

Version 1.6 records what each process runs in socketowner, so process lookup no
longer needs `/proc`. When updating from an earlier version, rebuild and install
socketowner too and add `find-process-bpf-only: true` to `config.yaml` after
`find-process-bpf-map`:

```sh
sudo systemctl daemon-reload
sudo systemctl restart socketowner.service
sudo systemctl restart mihomo.service
```

Without the BPF LSM, follow [Through /proc](#through-proc) instead.

If `restorecon` doesn't relabel `ruleset/` to `mihomo_data_t`, check
`sudo semanage fcontext -l -C` for local rules on `/etc/mihomo` and remove them with
`sudo semanage fcontext -d`; local rules override the module's.

## License

Licensed under the [GNU GPL v3](LICENSE).
