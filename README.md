Safe Connection to the Network via VPN 
A censorship-resistant VPN with web-based administration 
Abstract 
This report describes the design and implementation of a censorship-resistant Virtual Private 
Network. The goal is not merely to provide an encrypted tunnel, but to provide a tunnel whose 
existence cannot be easily detected by a network-level adversary performing deep packet 
inspection and active probing. The system uses TLS 1.3 as its outer cryptographic envelope, the 
VLESS protocol as its inner transport, and two complementary anti-fingerprinting layers: the 
REALITY transport with XTLS Vision flow control, and a WebSocket-over-CDN fallback. A 
small web dashboard manages user keys, regenerates the server configuration atomically, and 
records aggregate traffic statistics without logging destination addresses, applying the principle of 
data minimisation throughout. 
1. Introduction 
Conventional Virtual Private Networks such as OpenVPN, IPsec/IKEv2, and WireGuard were 
designed against a passive eavesdropper and against confidentiality and integrity threats on a 
generally cooperative network. They were not designed against an adversary that operates the 
network itself. In jurisdictions where the national network is centrally inspected and selectively 
filtered, off-the-shelf VPNs fail in a characteristic way: the cryptography is unbroken, yet the 
connection is dropped, throttled, or its endpoint is blocked, because the protocol’s wire signature 
is recognisable as a VPN. 
This project asks the next question: how can a VPN remain secure, and additionally appear, on the 
wire, to be something that the censor is unwilling to block, such as ordinary web traffic to a major 
website. The system described here was deployed and tested under realistic conditions, from 
clients in Italy and Iran, and is intended as an educational case study in applied network security. 
2. Threat Model 
The adversary considered is a state-level network operator with full control over the access 
network between the user and the open Internet. Specifically, the adversary is assumed to be able 
to: 
✓ Inspect every packet that crosses the network boundary (Deep Packet Inspection). 
✓ Selectively block destination IP addresses and Server Name Indication (SNI) values. 
✓ Send arbitrary probing connections to any server (active probing). 
✓ Throttle individual flows based on observed traffic patterns. 
✓ Maintain block-lists of fingerprints and rotate them over time. 
The adversary is NOT assumed to be able to: 
➢ Break TLS 1.3, ECDHE, or modern AEAD ciphers (this is a cryptographic, not a 
network, assumption). 
➢ Compromise the user’s device or the VPN server. 
➢ Coerce the chosen camouflage website (e.g. Google) into cooperating. 
The security goals are, therefore: 
✓ Confidentiality and integrity of user traffic (a conventional VPN goal). 
✓ Indistinguishability of the connection from ordinary HTTPS to a major site (the 
unconventional goal). 
✓ Resistance to active probing: a probe of the server should not reveal a VPN. 
✓ Data minimization: even the operator of the VPN should not be able to reconstruct who 
visited what site. 
3. Cryptographic Building Blocks 
Before discussing the protocols, it is useful to name the cryptographic primitives that the system 
relies on. All of them are public, peer-reviewed, and standardised. 
3.1 Transport Layer Security 1.3 (RFC 8446) 
TLS is the protocol that protects most of the modern web. Version 1.3, published in 2018, is the 
current standard. It performs an authenticated key exchange between client and server, after which 
all further traffic is encrypted and integrity-protected. Compared to earlier versions, TLS 1.3 has 
shorter handshakes (one round-trip instead of two), removes weak ciphers, and ALWAYS provides 
forward secrecy: even if the server’s long-term private key is later compromised, past sessions 
cannot be decrypted. 
3.2 Elliptic Curve Diffie–Hellman Ephemeral (ECDHE) 
ECDHE is the key-agreement algorithm used inside TLS 1.3. Each side picks a fresh, random 
private value, computes a corresponding public point on an elliptic curve, and they exchange these 
public points. From them, both sides independently derive the same shared secret — but a passive 
observer of the wire sees only the public points and cannot compute the secret in any feasible 
amount of time, under the elliptic-curve discrete-logarithm assumption. The “Ephemeral” in the 
name means the private values are thrown away after the handshake; this is what provides forward 
secrecy. 
3.3 Authenticated Encryption with Associated Data (AEAD) 
Once the keys are derived, the actual bulk encryption is performed with an AEAD cipher. AEAD 
provides confidentiality (an attacker cannot read the data) AND integrity (an attacker cannot 
modify the data without detection), in a single primitive. The system uses two such ciphers, both 
negotiated by TLS 1.3: 
➢ AES-256-GCM — the standard Advanced Encryption Standard in Galois/Counter Mode 
(RFC 5288). 
➢ ChaCha20-Poly1305 — a stream cipher with a polynomial MAC (RFC 8439), preferred 
on devices without AES hardware acceleration. 
3.4 Curve25519 / X25519 (RFC 7748) 
Curve25519 is the specific elliptic curve used by both TLS 1.3’s ECDHE and the REALITY 
transport described later. It was designed by Daniel J. Bernstein specifically to be fast, side-channel 
resistant, and free of the parameter-selection concerns that have surrounded earlier curves. X25519 
is the key-agreement function built on it. The REALITY private key generated during deployment 
is an X25519 key. 
3.5 bcrypt (1999) 
bcrypt is a deliberately slow password-hashing function. It is used to store the administrator 
password for the management dashboard. Unlike general-purpose hashes such as SHA-256, bcrypt 
has a tunable cost parameter that makes brute-force search expensive even with modern hardware. 
The dashboard never sees the plaintext password after deployment; only the bcrypt hash is stored, 
and each login attempt verifies in constant time. 
# Constant-time admin password verification (FastAPI): 
from passlib.context import CryptContext 
from secrets import compare_digest 
 
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto") 
 
def verify_admin(credentials): 
    user_ok = compare_digest(credentials.username, ADMIN_USER) 
    pass_ok = pwd_context.verify(credentials.password, ADMIN_PASS_HASH) 
    if not (user_ok and pass_ok): 
        raise HTTPException(401, "Invalid credentials") 
    return credentials.username 
 
Two security details are worth noting in this code. First, the username comparison uses 
secrets.compare_digest, not the == operator, to avoid timing side-channels that could leak the 
username length to a remote attacker measuring response times. Second, the bcrypt verify itself is 
constant-time for a given hash, so a wrong password leaks no information beyond “wrong”. 
4. The Protocol Layer 
4.1 VLESS 
VLESS is a stateless transport protocol developed within the XTLS/Xray-core project. Its purpose 
is to carry user traffic inside an outer secure channel. Each VLESS connection begins with a tiny 
header containing the user’s UUID, the requested destination, and an optional “flow” directive; 
everything that follows is the user’s actual traffic. Critically, VLESS itself performs NO 
encryption. This is intentional and is a frequent source of confusion. The setting encryption=none 
does not mean the traffic is unencrypted; it means that VLESS does not add a second redundant 
layer of encryption on top of the outer TLS 1.3 channel that already protects the connection. 
Double encryption (such as the older VMess protocol used) was found to be observable on the 
wire as a distinctive packet-size pattern, allowing a censor to identify the protocol even though 
they could not decrypt it. By NOT encrypting, VLESS removes that fingerprint. The security 
argument is therefore: VLESS is safe to run with encryption=none because, and only because, it 
is carried inside TLS. 
4.2 The REALITY Transport 
REALITY is the system’s core anti-DPI mechanism. It allows the VPN server to present itself, to 
any outside observer including the censor, as a different, completely legitimate website. In this 
deployment the impersonated target is www.google.com. 
The handshake works as follows: 
•  A connection arrives on the VPN server’s REALITY port (8443). The server cannot yet tell 
whether it is a legitimate VPN client or a probe. 
•  The server performs a real TLS 1.3 handshake with the camouflage target 
(www.google.com) on behalf of the connection. The Server Hello returned by Google is 
forwarded to the connecting client. 
•  If the client is a legitimate VPN client, it knows the server’s X25519 public key. The client 
encodes an authentication token inside specific fields of its Client Hello that only it could 
have produced. The VPN server recognises this token and switches the connection to 
VPN mode. 
•  If the client is a probe — say, a censor sending arbitrary connections to discover whether 
92.4.216.155:8443 is a VPN — it does not know the X25519 key, the authentication 
fields look like noise, and the connection is transparently proxied through to Google. 
From the probe’s point of view, the IP simply hosts a website. 
REALITY borrows Google’s TLS identity. The server certificate the censor sees is Google’s real 
certificate, signed by a public Certificate Authority that the censor trusts. There is no self-signed 
certificate, no Let’s Encrypt for an unknown domain, no SNI mismatch — the standard signals of 
a self-hosted proxy are absent. 
The relevant server-side configuration is: 
// Xray server: VLESS + REALITY + Vision inbound on port 8443 
{ 
  "tag": "vless-reality", 
  "listen": "0.0.0.0", 
  "port": 8443, 
  "protocol": "vless", 
  "settings": { 
    "clients": [{ 
      "id": "<USER_UUID>", 
      "flow": "xtls-rprx-vision" 
    }], 
    "decryption": "none" 
  }, 
  "streamSettings": { 
    "network": "tcp", 
    "security": "reality", 
    "realitySettings": { 
      "dest": "www.google.com:443", 
      "serverNames": ["www.google.com"], 
      "privateKey": "<X25519_PRIVATE_KEY>", 
      "shortIds": ["<SHORT_ID>"] 
    } 
  } 
} 
 
4.3 XTLS Vision (xtls-rprx-vision) 
REALITY camouflages the handshake. Vision camouflages what happens after the handshake. 
When a VPN carries, for example, HTTPS traffic to YouTube inside TLS 1.3, an observer who 
cannot decrypt the outer layer can still measure the size of each inner record. Inner TLS records 
leave a characteristic length pattern that ordinary single-layer HTTPS does not. This is known as 
a TLS-in-TLS fingerprint, and modern DPI systems use machine learning to detect it. 
Vision works by splicing: when it detects that the inner traffic is also TLS, it stops re-wrapping the 
inner records and instead passes the encrypted bytes through the outer TLS connection in a way 
that flattens the length distribution. To the censor’s classifier, the traffic now looks the same as 
ordinary HTTPS to Google. The flow=xtls-rprx-vision setting on the client and server enables this 
behaviour. 
4.4 WebSocket over CDN (the second tunnel) 
The system also runs a second tunnel on port 443: VLESS carried inside a WebSocket inside TLS 
1.3, fronted by Cloudflare. This tunnel does not need REALITY because it borrows a different 
kind of camouflage: the connection is destined for a Cloudflare edge IP, indistinguishable from the 
millions of legitimate websites Cloudflare hosts. The censor cannot block the destination IP 
without also blocking those other sites — a high-collateral-damage decision. 
Nginx terminates TLS at the server. A connection that requests the secret path /tunnel2026 is 
forwarded to the local Xray instance. ANY OTHER REQUEST is transparently proxied to a real 
third-party website (Wikipedia), which is the active-probing defence: a censor that probes the 
server with a normal browser will simply appear to receive Wikipedia content. 
# Nginx: TLS termination, secret-path VPN, active-probe decoy 
server { 
    listen 443 ssl; 
    server_name securetunnelforcomet.xyz; 
    ssl_certificate     /etc/letsencrypt/.../fullchain.pem; 
    ssl_certificate_key /etc/letsencrypt/.../privkey.pem; 
    ssl_protocols TLSv1.3; 
 
    # VLESS-over-WebSocket on a secret path 
    location /tunnel2026 { 
        proxy_pass http://127.0.0.1:10000; 
        proxy_http_version 1.1; 
        proxy_set_header Upgrade $http_upgrade; 
        proxy_set_header Connection "upgrade"; 
        proxy_read_timeout 86400s; 
    } 
 
    # Anything else: pretend to be a real website 
    location / { 
        proxy_pass https://www.wikipedia.org; 
        proxy_ssl_server_name on; 
        proxy_set_header Host www.wikipedia.org; 
    } 
} 
 
5. System Architecture 
The deployed system consists of three planes: the data plane (the two VPN tunnels), the control 
plane (the Xray gRPC management API and the configuration file), and the management plane (a 
web dashboard the operator uses to add and revoke users). 
The two tunnels are intentionally complementary. The REALITY tunnel (port 8443, direct) is the 
one designed to survive aggressive DPI. The WebSocket-over-Cloudflare tunnel (port 443, via 
CDN) hides the origin IP address entirely and is the appropriate fallback when REALITY’s IP 
becomes graylisted. Both tunnels share the same set of user UUIDs, so a single user holds a single 
identity across both connection methods. 
5.1 Authoritative user list 
The set of users authorised to use the VPN is stored in PostgreSQL, not in the Xray configuration 
file. The Xray configuration is regenerated from the database whenever a user is added or revoked. 
This separation lets the operator reason about user state independently of the running server, makes 
revocation a single database operation, and keeps a permanent audit record. -- Authoritative user record (PostgreSQL) 
CREATE TABLE vpn_users ( 
    id          SERIAL PRIMARY KEY, 
    uuid        UUID NOT NULL UNIQUE, 
    email       TEXT NOT NULL UNIQUE, 
    label       TEXT, 
    inbounds    TEXT[] NOT NULL DEFAULT 
                ARRAY['vless-reality','vless-ws'], 
    active      BOOLEAN NOT NULL DEFAULT TRUE, 
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(), 
    revoked_at  TIMESTAMPTZ 
); 
 
Revocation is soft-delete: the row is marked inactive, but kept, so that the historical traffic record 
for that user remains attributable. This is itself a security property: an audit can answer “who was 
authorised on day X” even after the user has been removed from the live configuration. 
5.2 Client link generation 
When the operator adds a user, the dashboard generates a self-contained client link that encodes 
the user’s UUID and all the public REALITY parameters. The user imports this link into a client 
application (V2Box, NPV Tunnel, v2rayNG). The link is the SHARE format; everything in it is 
public except the UUID, which functions as a bearer credential. An example REALITY link has 
the structure: 
vless://<UUID>@<server-ip>:8443 
  ?encryption=none 
  &flow=xtls-rprx-vision 
  &security=reality 
  &sni=www.google.com 
  &fp=chrome 
  &pbk=<X25519_PUBLIC_KEY> 
  &sid=<SHORT_ID> 
  &type=tcp 
#<USER_LABEL> 
 
The pbk (public key) and sid (short ID) are the public counterparts of the private X25519 key and 
shortIds from the server config. The fp=chrome directive tells the client library to mimic Chrome’s 
TLS fingerprint, closing one more side-channel by which a censor could identify VPN client 
traffic. 
6. Operational Security 
6.1 Data minimisation 
An honest VPN service should be unable to answer the question “which websites did this user 
visit?”, because if it CAN answer that, then so can anyone who gains access to its logs by warrant, 
breach, or coercion. Access logging in Xray is therefore disabled by configuration. Only byte 
counters per user are collected, which is sufficient for the operator to enforce fair use but cannot 
reconstruct browsing history. 
// Log configuration: warnings and errors only, no access log 
"log": { 
  "loglevel": "warning", 
  "error": "/var/log/xray/error.log" 
} 
// Note the absence of an "access" field: this is intentional. 
 
6.2 Privilege separation via systemd path-watcher 
The management dashboard runs inside a Docker container. When the operator adds or revokes a 
user, the container needs to trigger a reload of the host’s Xray service. A naive solution would be 
to mount the host’s Docker socket inside the container or to grant it sudo. Both are extreme: a 
container with the Docker socket can run any privileged command on the host, defeating 
containerisation. 
A safer pattern is to let the container signal its intent by writing to a single file, and have systemd 
react on the host. The container needs only write access to that one file. The host runs a path
watcher unit that triggers a small one-shot service whenever the file is modified. 
# /etc/systemd/system/xray-reload.path 
[Unit] 
Description=Watch for reload signal from dashboard 
 
[Path] 
PathModified=/var/run/vpn-dashboard/reload-xray 
Unit=xray-reload.service 
 
[Install] 
WantedBy=multi-user.target 
 
# /etc/systemd/system/xray-reload.service 
[Unit] 
Description=Reload Xray when dashboard requests it 
After=xray.service 
 
[Service] 
Type=oneshot 
ExecStart=/bin/systemctl restart xray 
 
The container therefore has no direct path to root on the host: it cannot execute arbitrary 
commands, only request the one specific action that the path-unit allows. This is an application of 
the principle of least privilege at the container/host boundary. 
6.3 Atomic configuration regeneration with validated rollback 
When the user database changes, the dashboard rewrites the Xray configuration file. If the new 
file contains an error, Xray will fail to start, and all existing connections will be dropped without 
an obvious way to recover. The system therefore validates every new configuration before 
installing it and only swaps the live file atomically once validation has succeeded. 
# Atomic, validated configuration swap (simplified) 
tmp_path = write_new_config_to_temp(cfg) 
 
# 1. Validate by asking Xray itself if the file is acceptable 
result = subprocess.run( 
    ["xray", "run", "-test", "-config", tmp_path], 
    capture_output=True, timeout=10) 
if result.returncode != 0: 
    os.unlink(tmp_path) 
    return False, "Validation failed: " + result.stderr 
 
# 2. Back up the current live config 
shutil.copy2(XRAY_CONFIG_PATH, XRAY_CONFIG_PATH + ".prev") 
 
# 3. Set permissions for Xray's unprivileged user (nobody) 
os.chmod(tmp_path, 0o644) 
 
# 4. Atomic rename (POSIX guarantees no half-written state) 
os.replace(tmp_path, XRAY_CONFIG_PATH) 
 
# 5. Signal the host to reload Xray 
open(RELOAD_SIGNAL_PATH, "w").write("reload") 
 
Three security properties emerge from this sequence. Validation by Xray itself prevents a 
malformed file from ever being installed. The os.replace step is atomic on POSIX file systems, so 
an observer of the file can never see a partially-written configuration. The os.chmod step ensures 
the new file is readable by Xray’s non-root user (nobody) — the original implementation of this 
code missed that step, which caused Xray to fail to start with a permission-denied error on the new 
file; this real bug, and its fix, is exactly the kind of mundane operational mistake that produces a 
security incident if it goes unnoticed. 
7. Threats and Defences 
The following summary maps each adversary capability identified in Section 2 to the system 
component that defends against it. 
o Passive DPI of port 443 → indistinguishable from a normal HTTPS request to the 
Cloudflare-fronted domain; the secret path /tunnel2026 carries the VPN. 
o Passive DPI of port 8443 → REALITY handshake camouflage, the connection appears 
destined for www.google.com using Google’s real certificate. 
o Active probing of port 443 → the request is transparently served by Wikipedia. 
o Active probing of port 8443 → the connection without a valid REALITY token is 
proxied to Google. 
o TLS-in-TLS length fingerprint → XTLS Vision flattens the per-record length 
distribution. 
o Subpoena or compromise of the server → access logging disabled, only byte counters 
retained. 
o Compromise of one user’s device → soft-delete revocation rebuilds the Xray 
configuration without that UUID; other users unaffected. 
o Container escape from the management dashboard → the container has no Docker socket 
and no sudo; the only privileged operation it can request is restarting Xray. 
8. Limitations and Future Work 
Operational testing from Iran confirmed that connections succeed and traffic flows. However, 
several well-known limitations remain and are worth stating honestly. 
•  REALITY does not defend against IP graylisting. A datacenter IP that carries a sustained 
large volume of REALITY traffic can be selectively throttled or blocked by the censor, 
even though it cannot be identified as a VPN. The defence is periodic IP rotation, not a 
protocol change. 
•  The SNI/dest target (currently www.google.com) is a single point of fragility. A future 
improvement is rotation among a set of equally plausible targets and per-user dest 
selection. 
•  Cloudflare’s reachability from inside heavily censored networks is itself variable. The 
CDN fallback is not a guaranteed second path. 
•  The dashboard authenticates with HTTP Basic over TLS, which is appropriate for a single
operator system. A production deployment would replace it with session-based 
authentication, rate limiting, and an audit log of administrative actions. 
9. Conclusion 
The system demonstrates that a censorship-resistant VPN is achievable from open-source 
components, on commodity cloud infrastructure, and with a manageable code surface. The 
cryptography is conventional and well-understood — TLS 1.3, X25519, AEAD ciphers, bcrypt. 
The novelty lies entirely in the protocol layer: VLESS’s decision NOT to add redundant 
encryption, REALITY’s handshake camouflage, and Vision’s length-pattern flattening. Together 
these primitives produce a tunnel that is, in every respect a censor can observe, indistinguishable 
from ordinary web traffic to a major site. 
The wider lesson is that, against an adversary who controls the network, cryptographic strength 
alone is insufficient. The protocol must additionally choose what to look like. That choice — the 
choice of fingerprint — is now as much a security parameter as the cipher suite. 
References 
[1] E. Rescorla, “The Transport Layer Security (TLS) Protocol Version 1.3,” RFC 8446, Aug. 
2018. https://datatracker.ietf.org/doc/html/rfc8446 
[2] Y. Nir and A. Langley, “ChaCha20 and Poly1305 for IETF Protocols,” RFC 8439, Jun. 2018. 
https://datatracker.ietf.org/doc/html/rfc8439 
[3] A. Langley, M. Hamburg, and S. Turner, “Elliptic Curves for Security,” RFC 7748, Jan. 2016. 
https://datatracker.ietf.org/doc/html/rfc7748 
[4] J. Salowey, A. Choudhury, and D. McGrew, “AES Galois Counter Mode (GCM) Cipher Suites 
for TLS,” RFC 5288, Aug. 2008. https://datatracker.ietf.org/doc/html/rfc5288 
[5] N. Provos and D. Mazières, “A Future-Adaptable Password Scheme,” USENIX Annual 
Technical Conference, 1999. https://www.usenix.org/legacy/events/usenix99/provos.html 
[6] XTLS Project, “Xray-core repository and documentation,” 2026. 
https://github.com/XTLS/Xray-core 
[7] XTLS Project, “REALITY transport — design and rationale,” 2026. 
https://github.com/XTLS/REALITY 
[8] XTLS Project, “VLESS protocol specification,” Project X Documentation, 2026. 
https://xtls.github.io/en/config/inbounds/vless.html 
[9] I. Fette and A. Melnikov, “The WebSocket Protocol,” RFC 6455, Dec. 2011. 
https://datatracker.ietf.org/doc/html/rfc6455 
[10] R. Sommese et al., “Measuring the Adoption and Actual Performance of TLS 1.3 in the Wild,” 
ACM Internet Measurement Conference, 2021.
