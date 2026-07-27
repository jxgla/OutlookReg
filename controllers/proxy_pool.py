"""代理池（pool 模式）：解析 + 顺序轮换 + 本地转发器（前置代理链）。

为什么需要本地转发器：
- Chrome/Chromium **不支持带账密的 SOCKS5**（`--proxy-server` 不认 URL 内嵌账密，也不做 socks5 用户名/密码认证）。
  故对代理池里的认证代理，只能本机起一个「无认证 SOCKS5」监听给 Chrome 连，转发器再拿账密去拨上游。
- 墙内还要求：转发器先经 `front_proxy`(本地 7897) 出墙，再连代理池上游，否则裸 socket 直连墙外代理会失败。
  链路：`Chrome → 本地转发器 → front_proxy(出墙) → 代理池 socks5/http(账密) → 目标`。

本模块自包含（socks5 服务端 + socks5/http 客户端都手写），不依赖 PySocks；
PySocks 仅在上层用 requests 走 socks5 做连通性测试时需要。
"""
import os
import base64
import socket
import threading
from urllib.parse import quote


# ============================================================
# 解析
# ============================================================
def _norm_scheme(sch, default):
    s = (sch or '').strip().lower()
    if s in ('socks5', 'socks5h', 'socks', 'socks4', 'socks4a'):
        return 'socks5'
    if s in ('http', 'https'):
        return 'http'
    return default


def _split_hostport(hp):
    host, port = hp.rsplit(':', 1)
    return host, port


def parse_pool_line(line, default_type='socks5'):
    """解析一行代理，兼容两种格式 + 可选 scheme:// 前缀。

    - 有 `@`：`[scheme://]user:pass@host:port`
    - 无 `@`：`[scheme://]host:port[:user:pass]`
    返回 dict{scheme,host,port,user,pass,raw,requests_url}；空行/`#` 注释返回 None。
    """
    raw = (line or '').strip()
    if not raw or raw.startswith('#'):
        return None
    s = raw
    scheme = default_type
    if '://' in s:
        sch, s = s.split('://', 1)
        scheme = _norm_scheme(sch, default_type)

    user = pw = ''
    try:
        if '@' in s:
            creds, hostport = s.rsplit('@', 1)
            if ':' in creds:
                user, pw = creds.split(':', 1)
            else:
                user = creds
            host, port = _split_hostport(hostport)
        else:
            parts = s.split(':')
            if len(parts) == 2:
                host, port = parts[0], parts[1]
            elif len(parts) >= 4:
                host, port, user = parts[0], parts[1], parts[2]
                pw = ':'.join(parts[3:])
            else:
                return None
        port = int(port)
    except Exception:
        return None
    if not host or port <= 0 or port > 65535:
        return None

    entry = {'scheme': scheme, 'host': host, 'port': port,
             'user': user, 'pass': pw, 'raw': raw}
    entry['requests_url'] = _build_requests_url(entry)
    return entry


def _build_requests_url(entry):
    """给 requests 用的上游 url（socks5h 远端 DNS / http）。"""
    scheme = 'socks5h' if entry['scheme'] == 'socks5' else 'http'
    auth = ''
    if entry['user']:
        auth = f"{quote(entry['user'], safe='')}:{quote(entry['pass'], safe='')}@"
    return f"{scheme}://{auth}{entry['host']}:{entry['port']}"


class _Front:
    __slots__ = ('scheme', 'host', 'port', 'user', 'pw')

    def __init__(self, scheme, host, port, user, pw):
        self.scheme, self.host, self.port, self.user, self.pw = scheme, host, port, user, pw


def parse_front_proxy(url):
    """解析前置代理 url（如 http://127.0.0.1:7897 或 socks5://... 或裸 host:port）。空 → None。"""
    if not url:
        return None
    s = str(url).strip()
    if not s:
        return None
    scheme = 'http'
    if '://' in s:
        sch, s = s.split('://', 1)
        scheme = _norm_scheme(sch, 'http')
    user = pw = ''
    if '@' in s:
        creds, s = s.rsplit('@', 1)
        if ':' in creds:
            user, pw = creds.split(':', 1)
        else:
            user = creds
    try:
        host, port = s.rsplit(':', 1)
        return _Front(scheme, host, int(port), user, pw)
    except Exception:
        return None


# ============================================================
# 代理池（有序轮换 + 坏代理/坏IP 跳过）
# ============================================================
class ProxyPool:
    def __init__(self, pool_file, default_type='socks5'):
        self.pool_file = pool_file
        self.default_type = default_type
        self._entries = []
        self._cursor = 0
        self._bad_proxies = set()   # raw 行
        self._bad_ips = set()       # 出口 IP
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if not os.path.exists(self.pool_file):
            raise FileNotFoundError(
                f"代理池文件不存在: {self.pool_file}（请在 config.json 同级创建 proxypool.txt）"
            )
        entries, seen = [], set()
        with open(self.pool_file, 'r', encoding='utf-8') as f:
            for line in f:
                e = parse_pool_line(line, self.default_type)
                if e and e['raw'] not in seen:
                    seen.add(e['raw'])
                    entries.append(e)
        if not entries:
            raise ValueError(f"代理池为空或无有效条目: {self.pool_file}")
        self._entries = entries

    def size(self):
        return len(self._entries)

    def next_entry(self):
        """顺序取下一条未标记为坏的代理；一整圈都不可用 → None（池已耗尽）。"""
        with self._lock:
            n = len(self._entries)
            for _ in range(n):
                e = self._entries[self._cursor % n]
                self._cursor = (self._cursor + 1) % n
                if e['raw'] in self._bad_proxies:
                    continue
                return e
            return None

    def mark_proxy_bad(self, raw):
        with self._lock:
            if raw:
                self._bad_proxies.add(raw)

    def mark_ip_bad(self, ip):
        with self._lock:
            if ip:
                self._bad_ips.add(ip)

    def is_ip_bad(self, ip):
        with self._lock:
            return bool(ip) and ip in self._bad_ips

    def stats(self):
        with self._lock:
            return {'total': len(self._entries),
                    'bad_proxies': len(self._bad_proxies),
                    'bad_ips': len(self._bad_ips)}

    def release(self):
        """整次运行结束后释放坏代理/坏IP 记录。"""
        with self._lock:
            self._bad_proxies.clear()
            self._bad_ips.clear()
            self._cursor = 0


# ============================================================
# socks5 服务端（给 Chrome）+ socks5/http 客户端（拨上游，含前置链）
# ============================================================
def _recv_exact(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('unexpected EOF')
        buf += chunk
    return buf


def _socks5_client_connect(sock, host, port, user='', pw=''):
    """在已连通的 sock 上，作为 socks5 客户端握手（可选账密）并 CONNECT 到 host:port。"""
    if user:
        sock.sendall(b'\x05\x02\x00\x02')     # 提供 NOAUTH + USERPASS
    else:
        sock.sendall(b'\x05\x01\x00')          # 仅 NOAUTH
    resp = _recv_exact(sock, 2)
    if resp[0] != 0x05:
        raise ConnectionError('bad socks5 greeting reply')
    method = resp[1]
    if method == 0x02:
        u = user.encode('utf-8'); p = pw.encode('utf-8')
        if len(u) > 255 or len(p) > 255:
            raise ConnectionError('socks5 cred too long')
        sock.sendall(b'\x01' + bytes([len(u)]) + u + bytes([len(p)]) + p)
        ar = _recv_exact(sock, 2)
        if ar[1] != 0x00:
            raise ConnectionError('socks5 auth failed')
    elif method == 0x00:
        pass
    else:
        raise ConnectionError(f'socks5 no acceptable auth method: {method}')

    hb = host.encode('utf-8')
    if len(hb) > 255:
        raise ConnectionError('socks5 host too long')
    req = b'\x05\x01\x00\x03' + bytes([len(hb)]) + hb + int(port).to_bytes(2, 'big')
    sock.sendall(req)
    rep = _recv_exact(sock, 4)
    if rep[1] != 0x00:
        raise ConnectionError(f'socks5 upstream connect failed rep={rep[1]}')
    atyp = rep[3]
    if atyp == 0x01:
        _recv_exact(sock, 4)
    elif atyp == 0x03:
        ln = _recv_exact(sock, 1)[0]
        _recv_exact(sock, ln)
    elif atyp == 0x04:
        _recv_exact(sock, 16)
    _recv_exact(sock, 2)  # BND.PORT


def _http_connect(sock, host, port, user='', pw=''):
    """在已连通的 sock 上，作为 HTTP 代理客户端发 CONNECT host:port（可选 Basic 认证）。"""
    lines = [f'CONNECT {host}:{port} HTTP/1.1', f'Host: {host}:{port}']
    if user:
        token = base64.b64encode(f'{user}:{pw}'.encode('utf-8')).decode('ascii')
        lines.append(f'Proxy-Authorization: Basic {token}')
    lines.append('Proxy-Connection: Keep-Alive')
    sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
    resp = b''
    while b'\r\n\r\n' not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError('http connect eof')
        resp += chunk
        if len(resp) > 65536:
            raise ConnectionError('http connect header too large')
    status = resp.split(b'\r\n', 1)[0]
    if b' 200' not in status:
        raise ConnectionError('http connect failed: ' + status.decode('latin-1', 'replace'))


def _dial_endpoint(host, port, front, timeout):
    """连到 host:port；有 front 则先经前置代理出墙建隧道。返回已连通 socket。"""
    if front is None:
        return socket.create_connection((host, port), timeout)
    raw = socket.create_connection((front.host, front.port), timeout)
    try:
        raw.settimeout(timeout)
        if front.scheme == 'socks5':
            _socks5_client_connect(raw, host, port, front.user, front.pw)
        else:
            _http_connect(raw, host, port, front.user, front.pw)
        return raw
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def _open_chain(entry, front, target_host, target_port, timeout):
    """建立完整链路 socket：[front→]entry(账密)→target。返回已 CONNECT 到 target 的 socket。"""
    s = _dial_endpoint(entry['host'], entry['port'], front, timeout)
    try:
        s.settimeout(timeout)
        if entry['scheme'] == 'socks5':
            _socks5_client_connect(s, target_host, target_port, entry['user'], entry['pass'])
        else:
            _http_connect(s, target_host, target_port, entry['user'], entry['pass'])
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


class LocalForwarder:
    """本地「无认证 SOCKS5」监听：Chrome 连它 → 转发器经前置链走上游出网。

    一实例一上游一本地端口；随浏览器起停。local_url 供 Chrome/requests 使用。
    """

    def __init__(self, entry, front_proxy='', timeout=20):
        self.entry = entry
        self.front = parse_front_proxy(front_proxy)
        self.timeout = timeout
        self._stopped = False
        self._lock = threading.Lock()
        self._conns = set()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', 0))
        self._srv.listen(128)
        self.port = self._srv.getsockname()[1]
        self.local_url = f'socks5://127.0.0.1:{self.port}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stopped:
            try:
                cli, _ = self._srv.accept()
            except OSError:
                break
            if self._stopped:
                try:
                    cli.close()
                except Exception:
                    pass
                break
            with self._lock:
                self._conns.add(cli)
            threading.Thread(target=self._handle_wrap, args=(cli,), daemon=True).start()

    def _handle_wrap(self, cli):
        try:
            self._handle_client(cli)
        finally:
            with self._lock:
                self._conns.discard(cli)

    def _handle_client(self, cli):
        up = None
        try:
            cli.settimeout(30)
            head = _recv_exact(cli, 2)
            if head[0] != 0x05:
                return
            _recv_exact(cli, head[1])            # methods
            cli.sendall(b'\x05\x00')              # 选 NOAUTH
            req = _recv_exact(cli, 4)
            ver, cmd, _rsv, atyp = req[0], req[1], req[2], req[3]
            if ver != 0x05 or cmd != 0x01:        # 仅支持 CONNECT
                cli.sendall(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
                return
            if atyp == 0x01:
                host = socket.inet_ntoa(_recv_exact(cli, 4))
            elif atyp == 0x03:
                ln = _recv_exact(cli, 1)[0]
                host = _recv_exact(cli, ln).decode('utf-8', 'replace')
            elif atyp == 0x04:
                host = socket.inet_ntop(socket.AF_INET6, _recv_exact(cli, 16))
            else:
                cli.sendall(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
                return
            port = int.from_bytes(_recv_exact(cli, 2), 'big')

            try:
                up = _open_chain(self.entry, self.front, host, port, self.timeout)
            except Exception:
                cli.sendall(b'\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00')  # 连接被拒
                return
            cli.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')      # 成功
            cli.settimeout(None)
            up.settimeout(None)
            self._pipe(cli, up)
        except Exception:
            pass
        finally:
            for s in (cli, up):
                try:
                    if s:
                        s.close()
                except Exception:
                    pass

    @staticmethod
    def _pipe(a, b):
        def fwd(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        t = threading.Thread(target=fwd, args=(a, b), daemon=True)
        t.start()
        fwd(b, a)
        t.join(timeout=1.5)

    def stop(self):
        self._stopped = True
        try:
            self._srv.close()
        except Exception:
            pass
        with self._lock:
            conns = list(self._conns)
            self._conns.clear()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
