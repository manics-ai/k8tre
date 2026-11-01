# Field Guide: Debugging Kubernetes & CNI Networking Failures

This guide outlines a systematic, step-by-step methodology for isolating and diagnosing complex Kubernetes networking issues involving Gateways, Ingress, CNI plugins (Cilium, Calico, Flannel), and host Linux kernel interactions.

---

## 1. The Diagnostic Philosophy: Divide and Conquer

When an HTTP request fails through a Gateway or Ingress, resist guessing. Break the network path into isolated segments and test each one independently:

```text
[Client] ──(1)──> [Gateway IP:Port] ──(2)──> [Proxy (Envoy)] ──(3)──> [ClusterIP/Pod IP] ──(4)──> [App Container]
```

### Check 1: External to Ingress / Gateway Handshake
Test whether TCP connects and TLS handshakes complete:
```bash
curl -vk https://<domain-or-ip>/ --max-time 10
```
- If it hangs before TLS or refuses connection: The Gateway listener is down, blocked by host firewall (iptables/nftables), or the IP/port is not bound.
- If TLS succeeds and returns `503 Service Unavailable` with `server: envoy`: The Gateway received the request, but failed to talk to the upstream.

### Check 2: Direct Pod and Service Health (Bypassing Gateway)
Test upstream reachability from the host or another pod:
```bash
# Test ClusterIP
curl -v http://<cluster-ip>:<service-port>/

# Test Pod IP directly
curl -v http://<pod-ip>:<container-port>/
```
- If direct curls succeed instantly with `200 OK`: **The application and core pod networking are healthy.** The problem is strictly in the path between the Gateway/Proxy and the upstream pod.

---

## 2. Inspecting the Proxy Layer (Envoy / Ingress)

When Envoy returns `503 upstream connect error or disconnect/reset before headers. reset reason: connection timeout`:

### Accessing Envoy Admin
If Envoy is managed by Cilium (`cilium-envoy`):
```bash
# Find Cilium agent pod
CILIUM_POD=$(kubectl -n kube-system get pods -l k8s-app=cilium -o jsonpath='{.items[0].metadata.name}')

# Check cluster health and failure stats
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg envoy admin clusters

# Check specific cluster connection failures
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg envoy admin clusters | grep -E "cx_connect_fail|rq_timeout"
```

### Enabling Real-Time Debug Logging in Envoy
Temporarily elevate logging to see upstream connection attempts:
```bash
# Enable debug logging
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg envoy admin logging set global debug

# Follow Envoy logs
kubectl -n kube-system logs -l k8s-app=cilium-envoy -f --tail=100

# Remember to reset back to info when finished!
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg envoy admin logging set global info
```

Look for:
```text
[debug][connection] [source/.../connection_impl.go:...] connecting to <pod-ip>:<port>
[debug][upstream] [source/.../connection_pool_base.cc:...] delayed connection failure: connection timeout
```

---

## 3. Investigating Host Socket States

Check whether the proxy actually sent the TCP SYN and what state the kernel socket is in:

```bash
# Look for upstream connection states to target port
ss -antp '( dport = :<target-port> or sport = :<target-port> )'

# Or look for sockets stuck in SYN-SENT
ss -antp | grep SYN-SENT
```

- `SYN-SENT`: The proxy kernel sent a TCP SYN, but never received or accepted a TCP `SYN-ACK`.
- `ESTABLISHED`: The TCP handshake completed; the issue is at the HTTP/TLS/application layer.
- No socket: The proxy failed to resolve DNS or aborted before socket creation.

---

## 4. Tracing Datapath Packets with CNI Tools

In Cilium, use `cilium-dbg monitor` to trace eBPF packet delivery:

```bash
# Find the Cilium Endpoint ID for the target pod
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg endpoint list

# Trace packets related to that endpoint (e.g. endpoint ID 123)
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg monitor -v --type trace --related-to <endpoint-id>
```

Watch for:
1. `-> endpoint <id> ... tcp SYN`: Did the pod receive the SYN?
2. `<- endpoint <id> ... tcp SYN, ACK`: Did the pod send back the SYN-ACK?
3. If both appear: **The pod answered!** The packet was passed to the host stack (`goto pass_to_stack`), but was dropped before reaching the listening socket.

---

## 5. Inspecting Host iptables & Packet Counters

When packets enter the host stack, check if iptables rules match or drop them:

```bash
# Inspect mangle table (common for mark and TPROXY operations)
kubectl exec -n kube-system $CILIUM_POD -- iptables -t mangle -L -v -n --line-numbers

# Check PREROUTING chains specifically
kubectl exec -n kube-system $CILIUM_POD -- iptables -t mangle -L CILIUM_PRE_mangle -v -n
```

**Key Technique - Counter Monitoring:**
Run `curl` against the failing gateway while watching packet counters:
```bash
kubectl exec -n kube-system $CILIUM_POD -- iptables -t mangle -L CILIUM_PRE_mangle -v -n
# Repeat curl...
kubectl exec -n kube-system $CILIUM_POD -- iptables -t mangle -L CILIUM_PRE_mangle -v -n
```
If a rule's packet counter increments on every failed request, you have identified the exact rule modifying or intercepting the packet.

---

## 6. Testing the Kernel Routing Policy Database (RPDB)

Linux policy routing uses rules (`ip rule`) and multiple routing tables (`ip route show table <id>`).

### List Rules & Tables
```bash
# List policy routing rules in evaluation order
ip rule show

# Inspect custom routing tables (e.g. Cilium proxy tables 2004, 2005)
ip route show table 2004
ip route show table local
```

### The Ultimate Diagnostic: `ip route get` with Ingress Simulation
You can ask the Linux kernel how it would route any packet, including specifying the incoming interface (`iif`), source, destination, and firewall mark (`mark`):

```bash
# Simulate packet arriving from pod on its lxc interface WITH firewall mark:
ip route get <host-dst-ip> from <pod-src-ip> iif <lxc-device> mark 0x200

# Compare WITHOUT mark:
ip route get <host-dst-ip> from <pod-src-ip> iif <lxc-device>
```

- If `ip route get` returns `RTNETLINK answers: Invalid argument`:
  **The Linux kernel FIB rejects this packet at ingress.** Any packet matching these criteria will be dropped immediately.

---

## 7. Checking Kernel Drops & Martian Packet Logging

When the Linux kernel drops packets due to reverse-path filtering (`rp_filter`) or invalid routing:

### Enable Martian Logging
```bash
# Enable logging for dropped invalid/martian packets
sudo sysctl -w net.ipv4.conf.all.log_martians=1
sudo sysctl -w net.ipv4.conf.default.log_martians=1

# Check kernel logs
dmesg -T | grep -i martian
journalctl -k -f | grep -i martian
```

### Critical Sysctls to Check
```bash
# Check reverse path filtering and source validation marks
sysctl -a | grep -E "src_valid_mark|rp_filter|accept_local"
```

| Parameter | Normal Default | Problematic State | Effect |
| :--- | :--- | :--- | :--- |
| `net.ipv4.conf.all.src_valid_mark` | `0` | `1` | Forces reverse-path checks to use `fwmark`. If `fwmark` routes to a local table, incoming packets from pods are flagged as martians and dropped. |
| `net.ipv4.conf.all.rp_filter` | `0` or `2` (loose) | `1` (strict) | Drops asymmetric packets if return route is not on incoming interface. |
| `net.ipv4.conf.<iface>.accept_local`| `0` | `0` | When `0`, packets with source IP resolving to local host arriving on an external interface are rejected. |

### The "Host Sysctl vs CNI Flags" Trap
When troubleshooting CNI proxy drops, remember:
1. **Redeploying the CNI or changing Helm flags (`bpf.tproxy: true`) does NOT reset host sysctls.** If a VPN client or daemon configured `net.ipv4.conf.all.src_valid_mark=1` in the kernel, the drop persists across CNI pod restarts.
2. **CNI Chaining can force legacy routing:** Configuring chaining modes like `cni.chainingMode=portmap` forces Cilium into legacy host routing mode, keeping host iptables mangle rules active.
3. **The Interface-Scoping Resolution Pattern:** Rather than disabling a required sysctl entirely (breaking the VPN client), scope it:
   ```bash
   sudo sysctl -w net.ipv4.conf.all.src_valid_mark=0
   sudo sysctl -w net.ipv4.conf.<vpn-interface>.src_valid_mark=1
   ```

---

## 8. Correlating Recent System Changes

If something worked "previously but not today", investigate host-level package and service changes:

```bash
# Debian/Ubuntu package upgrade history
grep " upgrade " /var/log/dpkg.log

# Check recent service restarts
systemctl list-units --type=service --state=running | grep -E "wireguard|netbird|tailscale|cilium|docker|k3s"

# Check system boot / restart times
uptime
who -b
```

Look specifically for VPN / mesh networking software (NetBird, Tailscale, WireGuard) or security daemon updates that inject policy routing rules or toggle kernel networking sysctls globally.

---

## 9. Preventing CNI Interface Hijacking

When running Kubernetes alongside VPNs or mesh tunnels (such as NetBird `wt0`, Tailscale `tailscale0`, or WireGuard `wg0`), CNIs configured with default automatic interface detection (`devices: ""`) can attempt to attach eBPF programs (TC/XDP) or calculate cluster MTUs based on the VPN interface.

### Symptoms of Interface Hijacking
- VPN tunnel goes down, disconnects, or experiences heavy packet loss as soon as the CNI starts.
- Pod MTU defaults to 1280 or 1420 (matching the VPN tunnel instead of the physical NIC).
- NodePort or service traffic gets intercepted on the VPN interface unintentionally.

### The Fix: Restrict CNI Device Discovery
Explicitly restrict the CNI to the node's physical interfaces and exclude VPN virtual interfaces in your Helm values:

```yaml
# Helm values.yaml
devices: "eth0,!wt*,!tailscale*"
```

Or via CLI:
```bash
--set devices="<physical-iface>,!<vpn-iface>*"
```

Verify which interfaces Cilium actively manages:
```bash
kubectl exec -n kube-system $CILIUM_POD -- cilium-dbg status --all-addresses
```
