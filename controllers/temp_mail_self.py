"""自托管 TempMail 客户端（Ryanlyjp/tempmail，Go API）。

与 controllers.temp_mail.TempMailClient 提供一致的调用面，作为 recovery_bind 的
另一个接码 provider。差异：
- 鉴权：`Authorization: Bearer tm_xxx`（管理员 API Key，见服务端 data/admin.key）。
- 建箱：POST /api/mailboxes {address?, domain?, source} → 201 {"mailbox": {id, full_address, ...}}
        address 空→服务端随机本地部分；domain 空→全局策略。本客户端支持「全随机/指定域名」。
- 取码：GET /api/mailboxes/:id/otp/latest → 200 {"otp": {code, received_at, ...}}
        服务端直接提取 OTP；404=暂无邮件，422=有邮件但没提到码。兜底再拉邮件正文自解析。
"""
import random
import string
import threading
import time
from datetime import datetime
import re

import requests

from controllers.temp_mail import TempMailClient

PROVIDER = "self"
_LOCAL_SANITIZE_RE = re.compile(r'[^a-z0-9._-]+', re.I)


def _parse_iso_ts(value):
    """把 RFC3339 时间字符串转成 unix 秒；失败返回 None（宁可不过滤也不误杀）。"""
    if not value:
        return None
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return None


class SelfTempMailClient:
    """线程安全：每任务独立 mailbox_id，取码只认本箱。"""

    def __init__(
        self,
        base_url="",
        api_key="",
        domain_mode="random",
        domain="",
        name_prefix="",
        timeout=30,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.domain_mode = (domain_mode or "random").strip().lower()
        self.domain = (domain or "").strip()
        self.name_prefix = (name_prefix or "").strip()
        self.timeout = timeout
        self._lock = threading.Lock()
        self.address = None
        self.mailbox_id = None
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": "OutlookRegister/1.0",
            "Authorization": f"Bearer {self.api_key}",
        })

    # ---------- 内部 ----------
    def _url(self, path):
        return f"{self.base_url}{path}"

    def _unique_name(self):
        base = (self.name_prefix or "").strip()
        base = _LOCAL_SANITIZE_RE.sub('', base).strip('._-').lower()
        if base:
            return base
        ts = time.strftime("%m%d%H%M%S")
        tid = abs(threading.get_ident()) % 10000
        rnd = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        return f"tm{ts}{tid:04d}{rnd}"

    def list_domains(self):
        """GET /api/domains?status=active → 激活域名列表（字符串）。"""
        try:
            resp = self._session.get(
                self._url("/api/domains"),
                params={"status": "active"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            doms = data.get("domains") or []
            out = []
            for d in doms:
                if not isinstance(d, dict):
                    continue
                name = d.get("domain")
                active = d.get("is_active", True) and (d.get("status", "active") == "active")
                if name and active:
                    out.append(name)
            return out
        except Exception:
            return []

    def _pick_domain(self):
        """按 domain_mode 决定建箱域名：fixed→配置域名；random→激活池随机；取不到则留空交服务端。"""
        if self.domain_mode == "fixed" and self.domain:
            return self.domain
        # random：优先客户端在激活域名池里随机（保证真随机，不依赖服务端默认策略）
        doms = self.list_domains()
        if doms:
            return random.choice(doms)
        # 兜底：配置里若有 domain 就用它，否则留空让服务端按 api_domain_strategy 选
        return self.domain or ""

    # ---------- 对外（与 TempMailClient 对齐） ----------
    def create_address(self, name=None, domain=None):
        """POST /api/mailboxes → 本实例独有 address + mailbox_id。"""
        chosen_domain = domain if domain is not None else self._pick_domain()
        payload = {"source": "api"}
        # name 留空 → 服务端随机本地部分；这里默认给个唯一名，避免撞箱且便于溯源
        payload["address"] = name or self._unique_name()
        if chosen_domain:
            payload["domain"] = chosen_domain
        resp = self._session.post(
            self._url("/api/mailboxes"),
            json=payload,
            timeout=self.timeout,
        )
        if resp.status_code == 409:
            # 撞名重试一次
            payload["address"] = self._unique_name()
            resp = self._session.post(self._url("/api/mailboxes"), json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json() or {}
        mb = data.get("mailbox") or {}
        with self._lock:
            self.address = mb.get("full_address") or mb.get("fullAddress")
            self.mailbox_id = mb.get("id")
        if not self.mailbox_id or not self.address:
            raise RuntimeError(f"self temp_mail create bad resp: {data}")
        return self.address, self.mailbox_id

    def _otp_latest(self):
        """GET /api/mailboxes/:id/otp/latest → (code, received_ts) 或 (None, None)。"""
        resp = self._session.get(
            self._url(f"/api/mailboxes/{self.mailbox_id}/otp/latest"),
            timeout=self.timeout,
        )
        if resp.status_code == 200:
            otp = (resp.json() or {}).get("otp") or {}
            return otp.get("code"), _parse_iso_ts(otp.get("received_at"))
        # 404 无邮件 / 422 有邮件没提到码 → 交给兜底
        return None, None

    def _otp_from_emails(self, after_ts=None):
        """兜底：拉最新邮件正文自解析（服务端 otp/latest 偶尔 422）。"""
        try:
            resp = self._session.get(
                self._url(f"/api/mailboxes/{self.mailbox_id}/emails"),
                params={"page": 1, "size": 5},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception:
            return None
        emails = data.get("emails") or data.get("results") or data.get("data") or []
        exclude = [self.address or "", (self.address or "").split("@")[0]]
        for mail in emails if isinstance(emails, list) else []:
            if not isinstance(mail, dict):
                continue
            if after_ts:
                ts = _parse_iso_ts(mail.get("received_at"))
                if ts is not None and ts + 2 < after_ts:
                    continue
            blob = "\n".join(str(mail.get(k) or "") for k in ("subject", "body_text", "body_html", "raw_message"))
            code = TempMailClient.extract_code_from_text(blob, exclude_substrings=exclude)
            if code:
                return code
        return None

    def wait_for_code(self, timeout_sec=120, poll_sec=3, after_ts=None, log=None):
        """轮询 otp/latest（含兜底）直到拿到验证码。after_ts：只认之后收到的信。"""
        if not self.mailbox_id:
            raise RuntimeError("self temp_mail: create_address first")
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                code, recv_ts = self._otp_latest()
                if code and (not after_ts or recv_ts is None or recv_ts + 2 >= after_ts):
                    if log:
                        log("temp_mail", f"otp/latest 取到 code={code} addr={self.address}", "OK")
                    return code
                # 兜底：正文自解析
                code2 = self._otp_from_emails(after_ts=after_ts)
                if code2:
                    if log:
                        log("temp_mail", f"正文兜底取到 code={code2} addr={self.address}", "OK")
                    return code2
            except Exception as exc:
                if log:
                    log("temp_mail", f"取码失败: {exc}", "WARN")
            time.sleep(poll_sec)
        if log:
            log("temp_mail", f"等待验证码超时 addr={self.address}", "FAIL")
        return None

    def session_dict(self):
        """持久化会话，供 OAuth 冷登录复用同一邮箱接码。"""
        return {
            "provider": PROVIDER,
            "address": self.address,
            "mailbox_id": self.mailbox_id,
            "base_url": self.base_url,
            "api_key": self.api_key,
        }


def client_from_config(cfg):
    """从 config['temp_mail'] 的 self 段构建客户端。"""
    cfg = cfg or {}
    self_cfg = cfg.get("self") or {}
    return SelfTempMailClient(
        base_url=(self_cfg.get("base_url") or "").strip(),
        api_key=(self_cfg.get("api_key") or "").strip(),
        domain_mode=(self_cfg.get("domain_mode") or "random").strip(),
        domain=(self_cfg.get("domain") or "").strip(),
        name_prefix=(self_cfg.get("name_prefix") or "").strip(),
        timeout=int(cfg.get("timeout", 30)),
    )


def client_from_session(session, cfg):
    """用绑定阶段保存的 mailbox_id 重建客户端，收同一邮箱的码。"""
    if not session or not session.get("mailbox_id"):
        return None
    self_cfg = (cfg or {}).get("self") or {}
    client = SelfTempMailClient(
        base_url=session.get("base_url") or self_cfg.get("base_url", ""),
        api_key=session.get("api_key") or self_cfg.get("api_key", ""),
        timeout=int((cfg or {}).get("timeout", 30)),
    )
    client.address = session.get("address")
    client.mailbox_id = session.get("mailbox_id")
    return client
