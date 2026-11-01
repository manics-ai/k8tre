# Root Cause Analysis: Cilium Gateway API HTTPRoute 503 Connection Timeout

## Executive Summary

External requests to Kubernetes Gateway API HTTPRoutes (for example `portal.example.com` and `jupyterhub.example.com`) were failing with HTTP `503 Service Unavailable`:

```text
upstream connect error or disconnect/reset before headers. reset reason: connection timeout
```

The upstream workloads (e.g. backend pods and ClusterIP services) were healthy and responded immediately to direct requests. Downstream TLS handshakes with Envoy on the Gateway IP at port 443 succeeded.

The failure occurred because return TCP `SYN-ACK` packets from upstream pods were systematically dropped by the Linux kernel on the host, leaving Envoy's upstream sockets stuck in `SYN-SENT` until timing out after 5 seconds.

---

## Detailed Root Cause

### 1. Packet Flow Breakdown

```text
Client (curl)
    │  (TLS handshake on <gateway-ip>:443) -> SUCCESS
    ▼
Cilium Envoy (cilium-envoy pod, hostNetwork)
    │  Opens upstream TCP socket with IP_TRANSPARENT and mark 0x80b00 (identity: ingress)
    │  Sends TCP SYN (<cilium-host-ip>:PORT -> <pod-ip>:<container-port>) via cilium_host
    ▼
Upstream Pod (<pod-ip>:<container-port>)
    │  Receives SYN, generates TCP SYN-ACK (<pod-ip>:<port> -> <cilium-host-ip>:PORT)
    │  Cilium eBPF program (bpf_lxc.c) sees destination is host -> passes packet to host stack
    ▼
Host PREROUTING (iptables mangle CILIUM_PRE_mangle)
    │  Rule: ! -o lo -m socket --transparent ... -j MARK --set-xmark 0x200
    │  Matches because Envoy's upstream socket has IP_TRANSPARENT -> sets fwmark 0x200
    ▼
Linux Policy Routing
    │  Rule 9: from all fwmark 0x200/0xf00 lookup 2004
    │  Table 2004: local default dev lo proto kernel scope host
    ▼
Kernel Ingress Route Evaluation (ip_route_input_slow + fib_validate_source)
    │  net.ipv4.conf.all.src_valid_mark = 1 instructs kernel to evaluate source IP using fwmark 0x200
    │  Table 2004 resolves source IP as RTN_LOCAL
    │  Packet arrived on external interface (lxc...), but accept_local = 0
    │  Kernel classifies packet as "martian source" and rejects with -EINVAL!
    ▼
SYN-ACK Dropped -> Envoy socket stays in SYN-SENT -> Times out after 5s -> HTTP 503
```

### 2. The Components in Detail

1. **Envoy Upstream Transparent Sockets:**
   Cilium Envoy operates with `hostNetwork: true`. When connecting to upstream pod endpoints, Envoy applies the `IpTransparentSocketOption` and `CiliumMarkSocketOption(mark: 0x80b00)` (representing `reserved:ingress`). The outbound SYN packet leaves with the host's `cilium_host` IP as source.

2. **Pod SYN-ACK Delivery:**
   The pod receives the SYN and replies with `SYN-ACK`. Cilium's container eBPF hook (`bpf_lxc.c`) identifies the host IP as a host delivery (`ENDPOINT_MASK_HOST_DELIVERY`) and passes the packet into the host networking stack via the pod's `lxc` veth interface (`goto pass_to_stack`, mark=0).

3. **`CILIUM_PRE_mangle` Marks the Packet with `0x200`:**
   In the host's iptables `mangle` table, Cilium installs:
   ```bash
   -A CILIUM_PRE_mangle ! -o lo -m socket --transparent -m mark ! --mark 0xe00/0xf00 -m mark ! --mark 0x800/0xf00 -j MARK --set-xmark 0x200/0xffffffff
   ```
   Because Envoy's upstream socket has `IP_TRANSPARENT` set, the Linux `xt_socket` kernel module matches the incoming SYN-ACK. Because the packet mark is 0, the rule overwrites `fwmark` with `0x200` (`MARK_MAGIC_TO_PROXY`).

4. **Table 2004 (`RouteTableToProxy`):**
   Linux policy routing has:
   ```text
   9: from all fwmark 0x200/0xf00 lookup 2004
   ```
   Table 2004 contains:
   ```text
   local default dev lo proto kernel scope host
   ```
   Table 2004 is installed by Cilium's proxy routing manager (`pkg/proxy/routes.go`) whenever Gateway API or L7 proxy listeners are active, ensuring marked packets can be steered to local loopback proxy listeners.

5. **The Trap: `src_valid_mark = 1` and Reverse Path Filtering:**
   Under Linux, `net.ipv4.conf.all.src_valid_mark = 1` forces the kernel's reverse-path filtering / source validation (`fib_validate_source()`) to use the packet's firewall mark (`fwmark 0x200`) when validating where the packet came from.
   
   Because rule 9 points `fwmark 0x200` to table 2004 (`local default dev lo`), the kernel resolves the packet's source IP as `RTN_LOCAL` (local host delivery). But the packet actually arrived on an external virtual interface (`lxc...`). Because `accept_local` is `0`, the Linux kernel flags the packet as a **martian source** and returns `-EINVAL`.

   **Reproduction / Verification:**
   ```bash
   # Route lookup WITH mark 0x200:
   $ ip route get <cilium-host-ip> from <pod-ip> iif <lxc-device> mark 0x200
   RTNETLINK answers: Invalid argument    # <-- Kernel drops the packet!

   # Route lookup WITHOUT mark:
   $ ip route get <cilium-host-ip> from <pod-ip> iif <lxc-device>
   local <cilium-host-ip> from <pod-ip> dev lo table local 
       cache <local> iif <lxc-device>     # <-- Kernel accepts the packet!
   ```

---

## Why Cilium-Level Flags Alone Do Not Resolve It

It is tempting to attempt to fix this purely through Helm values (e.g., `--set bpf.tproxy=true` or removing `--set cni.chainingMode=portmap`). However:

1. **Table 2004 is Required for L7:**
   Cilium installs rule 9 and table 2004 whenever L7 proxying or Gateway API is enabled. This routing table is an integral part of Envoy local packet delivery.
2. **CNI Chaining (`portmap`) Forces Legacy Routing:**
   When CNI chaining is configured (`cni.chainingMode=portmap`), Cilium automatically falls back to `enable-host-legacy-routing: "true"`, keeping the iptables `CILIUM_PRE_mangle` rules active.
3. **The Kernel Drop is Host-Wide:**
   Crucially, no Cilium Helm value or pod restart modifies the host Linux kernel's sysctl parameter `net.ipv4.conf.all.src_valid_mark`. As long as `all.src_valid_mark = 1` remains set on the host, the kernel will evaluate return packets against table 2004 and drop them before they can reach Envoy.

---

## Broader Context (Beyond NetBird)

This is a known architectural interaction in Cilium's L7 proxy implementation (documented in upstream Cilium issues **#46260** and **#46284**):
- **Other VPN / Mesh software:** Tailscale (`tailscaled`) and WireGuard (`wg-quick`) enable `net.ipv4.conf.all.src_valid_mark = 1` for policy-based tunnel routing.
- **Other Cilium features affected:** Pod transparent DNS inspection (`toFQDNs`) and any L7 HTTP network policy redirecting traffic through Envoy fail with identical symptoms under these conditions.

---

## Recommended Solution: Interface Scoping & Isolation

### Step 1: Apply Interface-Scoped Sysctls (Immediate Fix)
You do not need to disable `src_valid_mark` completely for your VPN client (such as NetBird or WireGuard). The VPN client only requires `src_valid_mark = 1` on its own tunnel interface (e.g., `wt0`).

Disable `src_valid_mark` globally across all interfaces, then re-enable it specifically on the VPN interface:

```bash
# 1. Disable globally (restores Gateway API, Cilium lxc interfaces, and pod traffic)
sudo sysctl -w net.ipv4.conf.all.src_valid_mark=0

# 2. Re-enable specifically on the VPN tunnel interface (keeps the VPN functioning)
sudo sysctl -w net.ipv4.conf.<vpn-iface>.src_valid_mark=1
```
*(Replace `<vpn-iface>` with your VPN interface, e.g., `wt0` for NetBird)*.

The fix is immediate; traffic through Gateway API HTTPRoutes will resume working without requiring restarts of Cilium, Envoy, or upstream workloads.

### Step 2: Persist Across Reboots
To ensure settings survive reboots or interface restarts, create `/etc/sysctl.d/99-cilium-gateway.conf`:

```text
net.ipv4.conf.all.src_valid_mark = 0
net.ipv4.conf.default.src_valid_mark = 0
```

### Step 3: Restrict Cilium to Required Interfaces (`devices` Setting)
By default, Cilium automatically detects network devices (`devices: ""`). When a host runs VPN or overlay interfaces (such as `wt0` for NetBird or `tailscale0`), Cilium can mistakenly attach eBPF programs (like TC or NodePort BPF programs) to the VPN tunnel interface. This can hijack VPN traffic, break mesh tunnels, or cause incorrect MTU sizing across the cluster.

Explicitly restrict Cilium to your primary physical network interface in Helm values:

```bash
--set devices="<physical-iface>"
# Or using wildcards with explicit exclusion of VPN devices:
--set devices="eth+,!<vpn-iface>*"
```

Example in `values.yaml`:
```yaml
# Only manage physical devices; exclude VPN/overlay tunnels
devices: "eth0,!wt*,!tailscale*"
```

### Step 4: Cilium Chaining Clean-up (Optional)
If you have `kubeProxyReplacement=true`, Cilium handles `hostPort` natively via eBPF. You can remove `--set cni.chainingMode=portmap` during your next Helm update to avoid forcing Cilium into legacy host routing mode.
