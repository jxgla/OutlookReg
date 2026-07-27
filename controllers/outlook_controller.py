import os
import json
import time
import random
import math
import shutil
import threading
from faker import Faker

from controllers import dp_page as D
from controllers.proxy_pool import ProxyPool, LocalForwarder


class OutlookController:
    """
    Outlook 自动注册控制器。
    
    职责：浏览器管理、代理选择(IP加权)、注册流程、验证码突破。
    每个线程独立的浏览器实例，通过 thread_local 隔离。
    类变量在所有线程间共享（代理使用计数、IP表现追踪、统计）。
    """

    # === 类变量（所有线程共享）===
    _proxy_usage = {}      # 每个代理端口被选中的次数
    _proxy_config = None   # 代理配置缓存（只解析一次）
    _proxy_pool = None     # pool 模式：ProxyPool 单例（按 proxypool.txt 构建一次）
    _ip_tracker = {}       # IP表现追踪（仅内存，不持久化）
    _attempts = 0          # 累计验证码尝试次数
    _success = 0           # 累计验证码成功次数
    _ip_info_cache = {}    # IP地理信息缓存（避免重复查询ipinfo）
    _b2_attempts = {'click': 0, 'dblclick': 0, 'hold': 0}
    _b2_success = {'click': 0, 'dblclick': 0, 'hold': 0}
    _state_lock = threading.Lock()

    # 国家代码 → (locale, 默认时区)
    LOCALE_MAP = {
        'JP': ('ja-JP', 'Asia/Tokyo'),       'US': ('en-US', 'America/Chicago'),
        'HK': ('zh-HK', 'Asia/Hong_Kong'),   'SG': ('en-SG', 'Asia/Singapore'),
        'KR': ('ko-KR', 'Asia/Seoul'),       'GB': ('en-GB', 'Europe/London'),
        'DE': ('de-DE', 'Europe/Berlin'),    'FR': ('fr-FR', 'Europe/Paris'),
        'CA': ('en-CA', 'America/Toronto'),  'AU': ('en-AU', 'Australia/Sydney'),
        'TW': ('zh-TW', 'Asia/Taipei'),      'CN': ('zh-CN', 'Asia/Shanghai'),
        'BR': ('pt-BR', 'America/Sao_Paulo'),'IN': ('en-IN', 'Asia/Kolkata'),
        'NL': ('nl-NL', 'Europe/Amsterdam'), 'TH': ('th-TH', 'Asia/Bangkok'),
        'VN': ('vi-VN', 'Asia/Ho_Chi_Minh'), 'MY': ('ms-MY', 'Asia/Kuala_Lumpur'),
        'PH': ('en-PH', 'Asia/Manila'),      'ID': ('id-ID', 'Asia/Jakarta'),
    }

    def __init__(self, config_data):
        """初始化：加载配置 → 创建线程存储 → 初始化统计 → 解析代理"""
        # config.json 已在 main.py 读取并解析，直接传入 dict
        self.wait_time = config_data['bot_protection_wait'] * 1000  # 秒→毫秒
        self.max_captcha_retries = config_data['max_captcha_retries']
        self.captcha_strategy = config_data.get('captcha_strategy', 0)
        # PX「按住」验证码解法：'hold'=按住长条(默认，成功率稳定) / 'a11y'=无障碍备用(点小人图标→等进度条走完→点长条)
        # 容忍常见别名/手误：ally、accessibility、无障碍 都归一为 'a11y'
        _pm = str(config_data.get('px_solve_mode', 'hold')).strip().lower()
        self.px_solve_mode = 'a11y' if _pm in ('a11y', 'ally', 'accessibility', 'a11y-fallback', '无障碍') else 'hold'
        self.enable_oauth2 = config_data["oauth2"]['enable_oauth2']
        self.headless = config_data.get('headless', False)
        self.email_suffix = config_data['email_suffix']

        # DrissionPage：驱动本机真实 Chrome（反检测优于 bundled Chromium）
        browser_cfg = config_data.get('browser', {}) or {}
        # 可选：指定 chrome/edge 可执行文件路径；留空 → DP 自动探测系统 Chrome
        self.browser_path = (browser_cfg.get('path') or browser_cfg.get('channel') or '').strip() or None
        # 固定窗口尺寸档位（inner/outer 一致，避免 viewport 随机化指纹）
        ws = browser_cfg.get('window_size')
        if isinstance(ws, (list, tuple)) and len(ws) == 2:
            self._window_sizes = [(int(ws[0]), int(ws[1]))]
        else:
            self._window_sizes = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080)]
        user_data_root = (browser_cfg.get('user_data_root') or '').strip()
        # 有头时挂后台：启动后最小化窗口，不抢占前台/焦点（仍是真实 Chrome，反检测不变）。
        # headless 下无窗口，此项无意义。
        self.background_window = bool(browser_cfg.get('background', False))
        # 禁用图片加载省流量（Blink 层 imagesEnabled=false，图片根本不发请求）。
        # 注：表单/按钮/PX按压不依赖图片，一般不影响流程；若某些页面异常可关掉。
        self.block_images = bool(browser_cfg.get('block_images', False))
        self.browser_user_data_root = user_data_root or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'browser_profiles',
        )
        # 备用邮箱绑定（CF Temp Mail）：概率出现，非固定步骤
        # 注册后 / OAuth 中任一处弹出「保护帐户」页则绑定；未弹出则直接继续
        self.temp_mail_cfg = config_data.get('temp_mail', {}) or {}
        self.bind_recovery_email = bool(self.temp_mail_cfg.get('enabled', True))

        self.thread_local = threading.local()
        self.cleanup_lock = threading.Lock()
        self.failure_lock = threading.Lock()
        self.runtime_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.active_resources = []
        self.active_forwarders = []   # pool 模式：本地转发器（跨线程，程序结束统一 stop）
        self.log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'log')
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_path = os.path.join(
            self.log_dir,
            f"{time.strftime('%Y-%m-%d_%H-%M-%S')}_{os.getpid()}.txt"
        )
        self.log_plain("[Browser] mode=drissionpage (system Chrome) stealth=screenX+force")
        self.runtime_stats = {
            'started_at': time.time(),
            'submitted': 0,
            'running': 0,
            'succeeded': 0,
            'failed': 0,
        }

        self.failure_stats = {
            'ip_cant_open': 0,
            'ip_blocked': 0,
            'captcha_fail': 0,
            'captcha_btn2_never_appeared': 0,
            'captcha_btn2_appeared_but_failed': 0,
            'funcaptcha': 0,
            'timeout': 0,
            'register_page_open_fail': 0,
            'register_form_fail': 0,
            'mail_init_fail': 0,
            'oauth_login_timeout': 0,
            'oauth_consent_fail': 0,
            'oauth_code_fail': 0,
            'oauth_token_network_fail': 0,
            'oauth_token_fail': 0,
            'oauth_password_wrong': 0,
            'oauth_password_blocked': 0,
            'oauth_retry_exhausted': 0,
            'recovery_bind_fail': 0,
            'browser_launch_fail': 0,
            'browser_context_fail': 0,
            'browser_page_fail': 0,
            'playwright_runtime_fail': 0,
        }

        cls = type(self)
        if cls._proxy_config is None:
            cls._proxy_config = self._parse_proxy_config(config_data.get('proxy', {}))

    # ============================================================
    # IP 信息查询（国家 + 时区 + 坐标，带缓存）
    # ============================================================
    @classmethod
    def _get_ip_info(cls, proxy_url, requests_proxy_url=None, timeout=3, use_cache=True):
        """查询代理IP的地理信息：出口IP、国家代码、时区、GPS坐标。

        - proxy_url：缓存键 & 兜底的 requests 代理。
        - requests_proxy_url：实际给 requests 用的代理（pool 传本地转发器 socks5h 口以测**完整链路**）。
        连不通/失败时返回 ip=None，上层据此判连通性。
        """
        if not proxy_url and not requests_proxy_url:
            return {'country': '??', 'timezone': 'UTC', 'loc': None, 'ip': None}
        req_url = requests_proxy_url or proxy_url
        cache_key = proxy_url or requests_proxy_url
        if use_cache:
            with cls._state_lock:
                if cache_key in cls._ip_info_cache:
                    return cls._ip_info_cache[cache_key]
        info = {'country': '??', 'timezone': 'UTC', 'loc': None, 'ip': None}
        try:
            import requests
            r = requests.get('https://ipinfo.io/json',
                             proxies={'http': req_url, 'https': req_url},
                             timeout=timeout, headers={'Accept': 'application/json'})
            if r.status_code == 200:
                d = r.json()
                info = {
                    'country': d.get('country', '??'),
                    'timezone': d.get('timezone', 'UTC'),
                    'loc': d.get('loc', None),  # "35.68,139.76"
                    'ip': d.get('ip', None),
                }
        except Exception:
            pass
        if use_cache:
            with cls._state_lock:
                cls._ip_info_cache[cache_key] = info
        return info

    def bump_failure(self, *names):
        with self.failure_lock:
            for name in names:
                self.failure_stats[name] = self.failure_stats.get(name, 0) + 1

    def _reset_thread_runtime(self):
        self._stop_thread_forwarder()
        for attr in ('_proxy', '_ip_info', '_log_prefix'):
            if hasattr(self.thread_local, attr):
                delattr(self.thread_local, attr)

    def prepare_thread_context(self):
        proxy = getattr(self.thread_local, '_proxy', None)
        if not proxy:
            proxy = self._pick_proxy()
        info = getattr(self.thread_local, '_ip_info', None)
        if info is None:
            info = self._get_ip_info(proxy)
            self.thread_local._ip_info = info
        return proxy, info

    def set_task_prefix(self, task_num, total):
        """设置当前线程的日志前缀： [编号/总-国家-IP] 并缓存IP地理信息"""
        proxy, info = self.prepare_thread_context()
        exit_ip = getattr(self.thread_local, '_exit_ip', None)
        if exit_ip:
            ip_short = exit_ip   # pool：显示真实出口 IP，而非本地转发器端口
        else:
            ip_short = proxy.split('//')[-1] if '//' in proxy else proxy
        self.thread_local._log_prefix = f"[{task_num}/{total}-{info['country']}-{ip_short}]"

    def log_event(self, flow, level, stage, message, attempt=None):
        line = self._format_log_line(flow, level, stage, message, attempt=attempt)
        self.write_log_line(line)

    def make_logger(self, flow, attempt=None):
        def _logger(stage, message, level='INFO'):
            self.log_event(flow, level, stage, message, attempt=attempt)
        return _logger

    def _log(self, msg):
        self.log_event('TASK', 'INFO', 'general', msg)

    def _log_prefix_str(self):
        return getattr(self.thread_local, '_log_prefix', '')

    def _format_log_line(self, flow, level, stage, message, attempt=None):
        prefix = getattr(self.thread_local, '_log_prefix', '')
        attempt_part = f"[A{attempt}]" if attempt is not None else ""
        return f"{prefix}[{flow}]{attempt_part}[{level}] {time.strftime('%H:%M:%S')} | {stage} | {message}"

    def write_log_line(self, line):
        # 强制单行：Playwright Call log 等多行异常不得刷屏
        if line is None:
            return
        text = str(line).replace('\r\n', '\n').replace('\r', '\n')
        if '\n' in text:
            parts = [p.strip() for p in text.split('\n') if p.strip()]
            # 丢弃 Call log 明细行
            kept = []
            for p in parts:
                if p.startswith('Call log:') or p.startswith('- waiting') or p.startswith('- navigated') or p.startswith('- attempting'):
                    continue
                kept.append(p)
            text = ' | '.join(kept) if kept else parts[0]
            if len(text) > 400:
                text = text[:397] + '...'
        with self.log_lock:
            print(text, flush=True)
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(text + '\n')
                f.flush()

    def log_plain(self, message):
        self.write_log_line(message)

    def update_runtime_stats(self, **kwargs):
        with self.runtime_lock:
            self.runtime_stats.update(kwargs)

    def get_runtime_stats(self):
        with self.runtime_lock:
            return dict(self.runtime_stats)

    def set_progress_base(self, succeeded=0, failed=0, started_at=None):
        """跨批次累计基数：进度条连续，不因换批归零。"""
        with self.runtime_lock:
            self._progress_base_succeeded = int(succeeded or 0)
            self._progress_base_failed = int(failed or 0)
            self._progress_run_started_at = started_at if started_at is not None else time.time()
            self.runtime_stats['succeeded'] = self._progress_base_succeeded
            self.runtime_stats['failed'] = self._progress_base_failed
            self.runtime_stats['started_at'] = self._progress_run_started_at

    def note_task_finished(self, success, total_tasks):
        """任务结束更新计数；仅成功时立刻打印进度（勿等 clean_up）。

        字段为**跨批次累计**：成功数 | 当前进度(成功+失败) | 总数 | 成功率 | 总耗时
        """
        with self.runtime_lock:
            if success:
                self.runtime_stats['succeeded'] = self.runtime_stats.get('succeeded', 0) + 1
            else:
                self.runtime_stats['failed'] = self.runtime_stats.get('failed', 0) + 1
            succeeded = self.runtime_stats.get('succeeded', 0)
            failed = self.runtime_stats.get('failed', 0)
            started = (
                getattr(self, '_progress_run_started_at', None)
                or self.runtime_stats.get('started_at')
                or time.time()
            )
        if not success:
            return
        current = succeeded + failed
        total = max(int(total_tasks or 0), 1)
        elapsed = time.time() - started
        rate = succeeded / max(current, 1) * 100
        self.log_plain(
            f"[进度] 成功 {succeeded} | 当前 {current}/{total} | "
            f"成功率 {succeeded}/{current} ({rate:.0f}%) | 总耗时 {elapsed / 60:.1f}min"
        )

    @classmethod
    def reset_shared_state(cls):
        with cls._state_lock:
            cls._proxy_usage.clear()
            cls._ip_tracker.clear()
            cls._ip_info_cache.clear()
            cls._attempts = 0
            cls._success = 0
            cls._b2_attempts = {'click': 0, 'dblclick': 0}
            cls._b2_success = {'click': 0, 'dblclick': 0}

    def penalize_ip(self, penalty=4):
        """惩罚当前IP（OAuth2全部失败、账号不存在等严重错误时调用）。增加失败计数，影响后续代理选择权重。"""
        proxy = getattr(self.thread_local, '_proxy', '')
        key = proxy.split('//')[-1] if '//' in proxy else proxy
        if key:
            with self._state_lock:
                if key not in self._ip_tracker:
                    self._ip_tracker[key] = {'win': 0, 'total': 0}
                self._ip_tracker[key]['total'] += penalty
            self.log_event('PROXY', 'WARN', 'penalize', f"{key} 惩罚 +{penalty}")

    def fresh_proxy_url(self, exclude: str = "") -> str:
        # pool 模式：每任务已独占一条出口，token 重试复用当前本地转发器（socks5h 远端DNS，过墙）。
        if (self._proxy_config or {}).get('mode') == 'pool':
            return self.current_requests_proxy()
        previous_proxy = getattr(self.thread_local, '_proxy', None)
        previous_info = getattr(self.thread_local, '_ip_info', None)
        try:
            if hasattr(self.thread_local, '_proxy'):
                del self.thread_local._proxy
            if hasattr(self.thread_local, '_ip_info'):
                del self.thread_local._ip_info
            for _ in range(4):
                picked = self._pick_proxy()
                if not exclude or picked != exclude:
                    return picked
            return self._pick_proxy()
        finally:
            if hasattr(self.thread_local, '_proxy'):
                del self.thread_local._proxy
            if hasattr(self.thread_local, '_ip_info'):
                del self.thread_local._ip_info
            if previous_proxy:
                self.thread_local._proxy = previous_proxy
            if previous_info is not None:
                self.thread_local._ip_info = previous_info

    def _register_active_browser(self, browser):
        with self.cleanup_lock:
            self.active_resources.append(browser)

    def _unregister_active_browser(self, browser):
        with self.cleanup_lock:
            self.active_resources = [item for item in self.active_resources if item is not browser]

    @staticmethod
    def _browser_failure_key(stage, message):
        msg = (message or "").lower()
        if stage == 'launch':
            return 'browser_launch_fail'
        if stage == 'context':
            return 'browser_context_fail'
        return 'browser_page_fail'

    def _log_browser_failure(self, stage, exc):
        message = str(exc)
        failure_key = self._browser_failure_key(stage, message)
        self.bump_failure(failure_key)
        self.log_event('BROWSER', 'FAIL', stage, f"{message} | class={failure_key}")
        return failure_key

    def _dispose_thread_browser(self):
        browser = getattr(self.thread_local, 'browser', None)
        if browser:
            try:
                browser.quit(timeout=5, force=True, del_data=True)
            except Exception:
                pass
            self._unregister_active_browser(browser)
        self._stop_thread_forwarder()
        for attr in ('browser', 'tab'):
            try:
                delattr(self.thread_local, attr)
            except Exception:
                pass

    # ============================================================
    # 代理
    # ============================================================
    @classmethod
    def _parse_proxy_config(cls, pc):
        """解析代理配置：single / 端口池 / pool。返回带 mode 的配置 dict。"""
        mode = pc.get('mode', 'single')
        if mode == 'pool':
            pool_type = pc.get('pool_type', 'socks5')
            pool_file = (pc.get('pool_file') or 'proxypool.txt').strip()
            # 相对路径 → 相对项目根目录（与 config.json 同级）
            if not os.path.isabs(pool_file):
                root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                pool_file = os.path.join(root, pool_file)
            cls._proxy_pool = ProxyPool(pool_file, default_type=pool_type)
            return {
                'mode': 'pool',
                'pool_type': pool_type,
                'pool_file': pool_file,
                'front_proxy': (pc.get('front_proxy') or '').strip(),
            }
        proxy_type = pc.get('type', 'http')
        host = pc.get('host', '127.0.0.1')
        if mode == 'single':
            ports = [pc.get('single_port', 7890)]
        else:
            ports = list(range(pc.get('port_start', 24000), pc.get('port_end', 24064) + 1))
        return {'mode': mode, 'type': proxy_type, 'host': host, 'ports': ports,
                'max_per': pc.get('max_per_proxy', 20)}

    def _pick_proxy(self):
        """选择代理端口：two-step。pool 模式改走代理池（本地转发器链）。"""
        cfg = self._proxy_config
        if cfg.get('mode') == 'pool':
            return self._pick_proxy_pool(cfg)
        with self._state_lock:
            available = []
            for p in cfg['ports']:
                if self._proxy_usage.get(p, 0) >= cfg['max_per']:
                    continue
                key = f"{cfg['host']}:{p}"
                info = self._ip_tracker.get(key, {})
                total = info.get('total', 0)
                win = info.get('win', 0)
                fail = max(total - win, 0)
                if total >= 2 and win == 0:
                    continue
                if fail >= 4 and win * 2 < fail:
                    continue
                available.append(p)
            if not available:
                available = list(cfg['ports'])
                for p in available:
                    self._proxy_usage[p] = 0
            weights = []
            for p in available:
                key = f"{cfg['host']}:{p}"
                info = self._ip_tracker.get(key, {})
                total = info.get('total', 0)
                win = info.get('win', 0)
                fail = max(total - win, 0)
                rate = win / total if total else 0.5
                weight = ((1 + win * 4) / (1 + fail * 3)) * (max(rate, 0.05) ** 2)
                weights.append(max(0.01, weight))
            port = random.choices(available, weights=weights, k=1)[0]
            self._proxy_usage[port] = self._proxy_usage.get(port, 0) + 1
        proxy_url = f"{cfg['type']}://{cfg['host']}:{port}"
        self.thread_local._proxy = proxy_url
        return proxy_url

    def _pick_proxy_pool(self, cfg):
        """pool 模式取一条可用代理：顺序取 → 起本地转发器 → 测完整链路连通性/出口IP → 好则用。

        - 连不通 → mark_proxy_bad（此线本 run 内永久跳过）。
        - 出口IP 命中坏IP集合 → 停转发器换下一条（不永久封线，动态住宅会轮换IP）。
        - next_entry→None（全是死线）或试满上限仍无可用 → 判定池耗尽/暂不可用，返回 ''。
        """
        pool = type(self)._proxy_pool
        if pool is None:
            self.thread_local._pool_exhausted = True
            return ''
        if getattr(self.thread_local, '_pool_exhausted', False):
            return ''
        front = cfg.get('front_proxy', '')
        max_attempts = max(pool.size() * 2, 8)
        for _ in range(max_attempts):
            entry = pool.next_entry()
            if entry is None:
                self.thread_local._pool_exhausted = True
                self.log_event('PROXY', 'FAIL', 'pool',
                               '代理池所有条目连通性均失败，判定【代理池已耗尽】')
                return ''
            try:
                fwd = LocalForwarder(entry, front_proxy=front)
            except Exception as exc:
                pool.mark_proxy_bad(entry['raw'])
                self.log_event('PROXY', 'WARN', 'pool',
                               f"启动本地转发器失败，跳过 {entry['host']}:{entry['port']}: {exc}")
                continue
            test_url = f"socks5h://127.0.0.1:{fwd.port}"
            info = self._get_ip_info(entry['raw'], requests_proxy_url=test_url,
                                     timeout=15, use_cache=False)
            exit_ip = info.get('ip')
            if not exit_ip:
                fwd.stop()
                pool.mark_proxy_bad(entry['raw'])
                self.log_event('PROXY', 'WARN', 'pool',
                               f"连通性失败，记录并跳过 {entry['host']}:{entry['port']}"
                               f"（{'经 7897 前置' if front else '直连'}）")
                continue
            if pool.is_ip_bad(exit_ip):
                fwd.stop()
                self.log_event('PROXY', 'INFO', 'pool',
                               f"出口IP {exit_ip} 曾人机失败被标记，跳过换下一条")
                continue
            # 好代理：写 thread_local（_proxy 给 Chrome；_requests_proxy 给 requests，均走本地转发器链）
            self._register_active_forwarder(fwd)
            self.thread_local._proxy = fwd.local_url
            self.thread_local._requests_proxy = test_url
            self.thread_local._upstream = entry['requests_url']
            self.thread_local._exit_ip = exit_ip
            self.thread_local._ip_info = info
            self.thread_local._proxy_key = entry['raw']
            self.thread_local._forwarder = fwd
            self.thread_local._pool_exhausted = False
            self.log_event('PROXY', 'INFO', 'pool',
                           f"顺序取 {entry['host']}:{entry['port']} → 本地端口 {fwd.port}"
                           f"（{'经 7897 出墙' if front else '直连'}），出口IP={exit_ip} 国家={info.get('country')}")
            return fwd.local_url
        self.thread_local._pool_exhausted = True
        self.log_event('PROXY', 'FAIL', 'pool',
                       f"连续 {max_attempts} 次取代理均不可用（连不通或IP被标记），判定【代理池暂不可用】")
        return ''

    # ---- pool 模式辅助 ----
    def _register_active_forwarder(self, fwd):
        with self.cleanup_lock:
            self.active_forwarders.append(fwd)

    def _unregister_active_forwarder(self, fwd):
        with self.cleanup_lock:
            self.active_forwarders = [f for f in self.active_forwarders if f is not fwd]

    def _stop_thread_forwarder(self):
        fwd = getattr(self.thread_local, '_forwarder', None)
        if fwd is not None:
            try:
                fwd.stop()
            except Exception:
                pass
            self._unregister_active_forwarder(fwd)
        for attr in ('_forwarder', '_upstream', '_exit_ip', '_proxy_key', '_requests_proxy'):
            try:
                delattr(self.thread_local, attr)
            except Exception:
                pass

    def is_single_proxy_mode(self):
        return (self._proxy_config or {}).get('mode') == 'single'

    def is_pool_proxy_mode(self):
        return (self._proxy_config or {}).get('mode') == 'pool'

    def current_exit_ip(self):
        info = getattr(self.thread_local, '_ip_info', None) or {}
        return getattr(self.thread_local, '_exit_ip', None) or info.get('ip')

    def current_requests_proxy(self):
        return (getattr(self.thread_local, '_requests_proxy', None)
                or getattr(self.thread_local, '_proxy', ''))

    def record_bad_exit_ip(self, ip):
        """记录坏出口IP（人机连续失败）：下次 pick 检测到同 IP 会跳过。

        仅记 IP——动态住宅同一条线会轮换出不同 IP，故不永久封整条线。
        """
        pool = type(self)._proxy_pool
        if pool is not None and ip:
            pool.mark_ip_bad(ip)
            self.log_event('PROXY', 'INFO', 'pool', f"记录坏出口IP {ip}，本 run 内后续检测到即跳过")

    def pool_exhausted(self):
        return bool(getattr(self.thread_local, '_pool_exhausted', False))

    @classmethod
    def release_proxy_pool(cls):
        """整次运行结束：释放坏代理/坏IP 记录（不清空代理条目本身）。"""
        if cls._proxy_pool is not None:
            try:
                cls._proxy_pool.release()
            except Exception:
                pass

    # ============================================================
    # 浏览器管理
    # ============================================================
    def _resolve_timezone(self, info):
        """代理国家 → 时区；ipinfo 有效时区优先。"""
        country = (info or {}).get('country', '??')
        locale_map = OutlookController.LOCALE_MAP
        tz = locale_map.get(country, locale_map.get('US'))[1]
        raw_tz = (info or {}).get('timezone', 'UTC')
        if raw_tz and raw_tz != 'UTC':
            tz = raw_tz
        return tz

    def _make_fingerprint_seed(self, proxy_url):
        """保留：为独立临时目录/日志生成一个种子（DP auto_port 自管数据目录）。"""
        port_part = 0
        try:
            hostport = proxy_url.split('//')[-1]
            port_part = int(hostport.rsplit(':', 1)[-1])
        except Exception:
            pass
        seed = (int(time.time() * 1000) ^ (port_part * 2654435761) ^ random.getrandbits(32)) & 0x7FFFFFFF
        if seed == 0:
            seed = random.randint(1, 0x7FFFFFFF)
        return seed

    @staticmethod
    def clear_browser_profiles_dir(root, log_fn=None):
        """清空 browser_profiles 目录下全部内容（保留目录本身）。DP 用临时目录，此处为兜底清理。"""
        if not root:
            return 0
        try:
            os.makedirs(root, exist_ok=True)
        except Exception:
            return 0
        removed = 0
        try:
            names = os.listdir(root)
        except Exception:
            return 0
        for name in names:
            path = os.path.join(root, name)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                removed += 1
            except Exception:
                pass
        if log_fn:
            try:
                log_fn(f"[Cleanup] 已清空 browser_profiles 共 {removed} 项: {root}")
            except Exception:
                pass
        return removed

    def clear_browser_profiles_root(self, log=True):
        """清空本实例配置的 profile 根目录。"""
        root = getattr(self, 'browser_user_data_root', None)
        log_fn = self.log_plain if log and hasattr(self, 'log_plain') else None
        return self.clear_browser_profiles_dir(root, log_fn=log_fn)

    def launch_browser(self):
        """启动 DrissionPage 浏览器（本机真实 Chrome + 反检测）。

        返回 (browser, tab)；失败返回 (None, None)。
        每任务独立浏览器（新代理→新出口 IP）；auto_port 保证多线程安全。
        """
        try:
            proxy_url, info = self.prepare_thread_context()
            if self.is_pool_proxy_mode() and (self.pool_exhausted() or not proxy_url):
                self.log_event('BROWSER', 'FAIL', 'pool',
                               '代理池无可用出口，跳过启动浏览器')
                return None, None
            tz = self._resolve_timezone(info)
            window_size = random.choice(self._window_sizes)
            extra_args = ['--blink-settings=imagesEnabled=false'] if self.block_images else None
            try:
                browser, tab = D.build_browser(
                    proxy_url=proxy_url,
                    headless=self.headless,
                    window_size=window_size,
                    browser_path=self.browser_path,
                    extra_args=extra_args,
                )
            except Exception as exc:
                self._log_browser_failure('launch', exc)
                self.log_event('BROWSER', 'FAIL', 'launch_detail', f"启动浏览器失败: {exc}")
                return None, None

            self.log_event(
                'BROWSER', 'INFO', 'launch',
                f"exe=system-chrome stealth=screenX+force tz={tz} "
                f"win={window_size[0]}x{window_size[1]} proxy={proxy_url.split('//')[-1]}"
            )
            # 反检测准备：screenX 补丁 + 时区/语言/地理 CDP 覆盖 + eager 加载
            try:
                D.prepare_tab(tab, timezone=tz, locale='zh-CN', loc=info.get('loc'))
            except Exception as exc:
                self.log_event('BROWSER', 'WARN', 'prepare', f"tab 反检测准备异常: {exc}")

            # 挂后台：有头且开启 background 时最小化窗口，不抢前台焦点（反节流开关保证仍全速）
            if self.background_window and not self.headless:
                try:
                    D.minimize_window(tab)
                except Exception:
                    pass

            self._register_active_browser(browser)
            return browser, tab
        except Exception as e:
            self._log_browser_failure('launch', e)
            self.log_event('BROWSER', 'FAIL', 'launch_detail', f"启动浏览器失败: {e}")
            return None, None

    def get_thread_browser(self):
        """获取当前线程的浏览器。首次调用时创建，之后复用。线程隔离，各自独立。"""
        if not hasattr(self.thread_local, "browser"):
            browser, tab = self.launch_browser()
            if not browser:
                return None
            self.thread_local.browser = browser
            self.thread_local.tab = tab
        return self.thread_local.browser

    def get_thread_page(self):
        """返回当前线程可用的工作 tab（DrissionPage ChromiumTab）。"""
        browser = self.get_thread_browser()
        if not browser:
            return None
        tab = getattr(self.thread_local, 'tab', None)
        if tab is None:
            try:
                tab = browser.latest_tab
                _, info = self.prepare_thread_context()
                D.prepare_tab(tab, timezone=self._resolve_timezone(info),
                              locale='zh-CN', loc=info.get('loc'))
                self.thread_local.tab = tab
            except Exception as exc:
                self._log_browser_failure('page', exc)
                self._dispose_thread_browser()
                return None
        return tab

    def new_context_tab(self, timezone=None, loc=None):
        """在当前线程浏览器里开一个新 tab（同 context，cookie 共享），已完成反检测准备。"""
        browser = self.get_thread_browser()
        if not browser:
            return None
        try:
            tab = browser.new_tab()
        except Exception as exc:
            self._log_browser_failure('page', exc)
            return None
        if timezone is None:
            _, info = self.prepare_thread_context()
            timezone = self._resolve_timezone(info)
            loc = info.get('loc')
        D.prepare_tab(tab, timezone=timezone, locale='zh-CN', loc=loc)
        return tab

    def export_cookies(self, tab):
        """导出全部 cookie（替代 Playwright storage_state）。返回 list[dict]。"""
        try:
            return list(tab.cookies(all_domains=True, all_info=True))
        except Exception:
            return []

    def inject_cookies(self, tab, cookies):
        """把导出的 cookie 注入新 tab（先落到登录域再设置）。"""
        if not cookies:
            return False
        try:
            tab.set.cookies(cookies)
            return True
        except Exception:
            return False

    def clean_up(self, page=None, type="all_browser"):
        """
        资源清理。
        - done_browser: 关闭当前线程的浏览器（OAuth2重试前调用，确保下次拿新IP）
        - all_browser: 关闭所有活跃浏览器（程序结束时调用）
        """
        if type == "done_browser":
            self._dispose_thread_browser()
            self._reset_thread_runtime()
        elif type == "all_browser":
            with self.cleanup_lock:
                browsers = list(self.active_resources)
                self.active_resources.clear()
                forwarders = list(self.active_forwarders)
                self.active_forwarders.clear()
            for browser in browsers:
                try:
                    browser.quit(timeout=5, force=True, del_data=True)
                except Exception:
                    pass
            for fwd in forwarders:
                try:
                    fwd.stop()
                except Exception:
                    pass
            for attr in ('browser', 'tab'):
                try:
                    delattr(self.thread_local, attr)
                except Exception:
                    pass
            # 兜底清空 profiles 根目录（DP 临时目录已随 quit(del_data) 删除）
            try:
                self.clear_browser_profiles_root(log=True)
            except Exception:
                pass

    # ============================================================
    # 注册流程
    # ============================================================
    def outlook_register(self, page, email, password):
        """
        完整的Outlook注册流程。
        
        步骤：打开注册页 → 同意条款 → 填邮箱 → 填密码
        → 填生日 → 填姓名 → 提交 → 检测风控 → 通过验证码 → 等邮箱初始化
        
        返回: True(注册成功) 或 False(失败)
        """
        # 记录本次失败落在哪一阶段，供上层区分「人机验证失败」与「IP打不开/表单失败」
        # （只有人机失败才整体重试、并在耗尽后判定此IP不可用）
        try:
            self.thread_local._last_fail_stage = None
            self.thread_local._current_email_local = str(email or '').strip()
            self.thread_local._current_email_full = f"{email}{self.email_suffix}"
        except Exception:
            pass
        fake = Faker()
        lastname = fake.last_name()
        firstname = fake.first_name()
        year = str(random.randint(1999, 2007))
        month = str(random.randint(1, 12))
        day = str(random.randint(1, 25))

        try:
            page.get('https://outlook.live.com/mail/0/?prompt=create_account')
            if not D.q(page, 'text:同意并继续', timeout=30):
                raise TimeoutError('agree button not found')
            start_time = time.time()
            page.wait(0.1 * self.wait_time / 1000)
            D.click_sel(page, 'text:同意并继续', timeout=30)
        except Exception:
            self.bump_failure('ip_cant_open', 'register_page_open_fail')
            self._log("[Fail:IP] - IP质量不佳，无法打开Outlook注册页面，请换IP重试")
            return False

        try:
            D.disable_autofill(page)
            # 选择是 outlook还是hotmail
            if self.email_suffix == "@hotmail.com":
                D.click_sel(page, 'text:@outlook.com', timeout=10)
                opt = D.q(page, 'xpath://*[@role="option" and normalize-space(.)="@hotmail.com"]', timeout=5)
                if opt:
                    opt.click()

            # 填充邮箱
            email_input = page.ele('css:[aria-label="新建电子邮件"]', timeout=10)
            email_input.click()
            email_input.input(email, clear=True)

            # 点击 "下一步
            D.click_sel(page, '[data-testid="primaryButton"]', timeout=5)
            page.wait(0.02 * self.wait_time / 1000)

            # 填充密码（真实输入）
            pwd_input = page.ele('css:[type="password"]', timeout=10)
            pwd_input.input(password, clear=True)
            page.wait(0.02 * self.wait_time / 1000)

            # 点击 "下一步
            D.click_sel(page, '[data-testid="primaryButton"]', timeout=5)
            page.wait(0.03 * self.wait_time / 1000)

            # 填充出生的年份
            D.fill_sel(page, '[name="BirthYear"]', year, timeout=10)

            # 填充出生日期,实际上不会走 try，走的是Except。因为 有浮层的存在，
            try:
                # 填充月份
                page.wait(0.02 * self.wait_time / 1000)
                page.ele('css:[name="BirthMonth"]', timeout=1).select.by_value(month)

                # 填充日期
                page.wait(0.05 * self.wait_time / 1000)
                page.ele('css:[name="BirthDay"]', timeout=1).select.by_value(day)
            except Exception:

                # 填充月份
                D.click_sel(page, '[name="BirthMonth"]', timeout=5)
                page.wait(0.02 * self.wait_time / 1000)
                mo = D.q(page, f'xpath://*[@role="option" and normalize-space(.)="{month}月"]', timeout=5)
                if mo:
                    mo.click()
                page.wait(0.04 * self.wait_time / 1000)

                # 填充日期
                D.click_sel(page, '[name="BirthDay"]', timeout=5)
                page.wait(0.03 * self.wait_time / 1000)
                da = D.q(page, f'xpath://*[@role="option" and normalize-space(.)="{day}日"]', timeout=5)
                if da:
                    da.click()
                D.click_sel(page, '[data-testid="primaryButton"]', timeout=5)

            # 填充姓氏
            D.fill_sel(page, '#lastNameInput', lastname, timeout=10)
            page.wait(0.02 * self.wait_time / 1000)

            # 填充名字
            D.fill_sel(page, '#firstNameInput', firstname, timeout=10)

            if time.time() - start_time < self.wait_time / 1000:
                page.wait((self.wait_time - (time.time() - start_time) * 1000) / 1000)

            # 点击 "下一步
            D.click_sel(page, '[data-testid="primaryButton"]', timeout=5)
            D.wait_gone(page, 'span > [href="https://go.microsoft.com/fwlink/?LinkID=521839"]', timeout=22)
            page.wait(0.4)

            if D.count(page, 'text:一些异常活动') or D.count(page, 'text:此站点正在维护，暂时无法使用，请稍后重试。') > 0:
                self.bump_failure('ip_blocked')
                self._log("[Fail:IP] - 当前IP已被微软风控拦截，请更换IP重试")
                return False

            if D.count(page, 'iframe#enforcementFrame') > 0:
                self.bump_failure('funcaptcha')
                self._log("[Fail:Captcha] - 验证码类型为FunCaptcha而非按压验证码，当前IP暂不支持，请换IP重试")
                return False

            # 策略 2：只自动填表到验证码界面，验证码 + 进邮箱 + OAuth 全部由你手动
            if self.captcha_strategy == 2:
                return self._hand_off_at_captcha(page, email, password)

            # 验证码是否通过
            captcha_result = self.handle_captcha(page)
            # 没有通过，报错
            if not captcha_result:
                try:
                    self.thread_local._last_fail_stage = 'captcha'
                except Exception:
                    pass
                raise TimeoutError

            # 验证码通过后：跳过辅助邮箱 / 通行密钥拦截，进入邮箱
            if self._enter_mailbox_after_register(page):
                self._log(f'Success:Captcha] - {email}{self.email_suffix} 验证码通过，已进入邮箱。')
            else:
                self._log(
                    f'Success:Captcha] - {email}{self.email_suffix} 验证码通过，但未确认进入邮箱（已尝试跳过/直达）。'
                )

        except Exception:
            self.bump_failure('captcha_fail', 'register_form_fail')
            self._log("[Fail:Captcha] - 验证码未通过（已达最大重试次数），请换IP后重新注册")
            return False

        # 走到这里说明验证码过了，注册成功
        self._log(f'Success:Email Registration] - {email}{self.email_suffix}: {password}')

        # 如果不需要oauth2，则直接结束，返回true
        if not self.enable_oauth2:
            return True

        # 邮箱初始化 + cookie/SSO 沉淀：进 OAuth 前固定多等几秒
        # 证据：过早跳 authorize 常落到 #i0116；重开浏览器更糟
        # 注：新号邮箱首页常卡在「未初始化」很久（微软后台 provisioning 慢），
        #     原先死等 [aria-label="新邮件"] 就绪信号会白白阻塞。现改为固定等 30s
        #     直接进 OAuth（cookie/SSO 此时已沉淀，不依赖邮箱首页渲染完成）。
        mail_settle_ms = 30000
        self.log_event('REGISTER', 'INFO', 'mail_init',
                       f'跳过邮箱就绪探测，固定等待 {mail_settle_ms}ms 沉淀 cookie 后进 OAuth2')
        try:
            page.wait(mail_settle_ms / 1000)
        except Exception:
            pass
        return True
        # --- 旧逻辑：死等邮箱就绪信号（新号常卡未初始化，已停用）---
        # oauth_settle_ms = 7000
        # try:
        #     if not D.q(page, '[aria-label="新邮件"]', timeout=32):
        #         raise TimeoutError('mailbox not ready')
        #     self.log_event('REGISTER', 'INFO', 'mail_init', f'收件箱就绪，等待 {oauth_settle_ms}ms 沉淀 cookie')
        #     page.wait(oauth_settle_ms / 1000)
        #     return True
        # except Exception:
        #     self.bump_failure('mail_init_fail')
        #     self.log_event(
        #         'REGISTER', 'WARN', 'mail_init',
        #         f'邮箱未初始化，仍等待 {oauth_settle_ms}ms 后继续 OAuth2',
        #     )
        #     try:
        #         page.wait(oauth_settle_ms / 1000)
        #     except Exception:
        #         pass
        #     return True

    def _is_mailbox_url(self, page):
        try:
            url = page.url or ''
        except Exception:
            return False
        if 'outlook.live.com/mail/' not in url:
            return False
        # 注册入口不算已进入邮箱
        if 'prompt=create_account' in url:
            return False
        return True

    def _click_if_visible(self, page, sel, timeout_ms=2500):
        """可见则点击（sel 为 CSS/DP 选择器字符串）。"""
        return D.click_if_visible(page, sel, timeout=timeout_ms / 1000)

    def _try_bind_recovery_email(self, page):
        """保护帐户页：创建临时邮箱 → 填 #EmailAddress → 接码 → #iOttText。失败则调用方再 skip。"""
        if not self.bind_recovery_email:
            return False
        try:
            from controllers.recovery_bind import bind_recovery_email, is_protect_account_page, is_ott_code_page
        except Exception as exc:
            self.log_event('REGISTER', 'WARN', 'recovery', f'加载 recovery_bind 失败: {exc}')
            return False
        if not is_protect_account_page(page) and not is_ott_code_page(page):
            return False

        def _log(stage, message, level='INFO'):
            self.log_event('REGISTER', level, stage, message)

        cfg = dict(self.temp_mail_cfg or {})
        self_cfg = dict((cfg.get('self') or {}))
        local = str(getattr(self.thread_local, '_current_email_local', '') or '').strip()
        if local:
            cfg['name_prefix'] = local
            cfg['enable_prefix'] = True
            self_cfg['name_prefix'] = local
            cfg['self'] = self_cfg

        result = bind_recovery_email(page, cfg, log=_log, local_name=(local or None))
        # 兼容 (ok, session) 或旧版 bool
        if isinstance(result, tuple):
            ok, session = result[0], (result[1] if len(result) > 1 else None)
        else:
            ok, session = bool(result), None
        if ok:
            self.thread_local.recovery_email_bound = True
            self.thread_local.recovery_email_skipped = False
            if session:
                self.thread_local.recovery_mail_session = session
                self.log_event(
                    'REGISTER', 'INFO', 'recovery_session',
                    f"已保存辅助邮箱会话 addr={session.get('address')}",
                )
        else:
            self.bump_failure('recovery_bind_fail')
        return ok

    def _mark_recovery_skipped(self):
        """注册阶段未绑定、点了暂时跳过 → OAuth 仍可能再弹保护帐户页。"""
        if not getattr(self.thread_local, 'recovery_email_bound', False):
            self.thread_local.recovery_email_skipped = True

    def recovery_bind_status(self):
        """供 OAuth 判断：bound / skipped / session(address+jwt 冷登录接码用)。"""
        return {
            'bound': bool(getattr(self.thread_local, 'recovery_email_bound', False)),
            'skipped': bool(getattr(self.thread_local, 'recovery_email_skipped', False)),
            'session': getattr(self.thread_local, 'recovery_mail_session', None),
        }

    def _dismiss_post_register_intercepts(self, page):
        """注册成功后、进 mail/0 前：优先绑定辅助邮箱，再处理通行密钥。

        正常路径：验证码通过 →「让我们来保护你的帐户」→ 绑定临时邮箱+接码
        → 取消通行密钥 → mail/0。绑定成功后 OAuth 通常不再出现该页。
        """
        acted = False

        # 1) 「让我们来保护你的帐户」：主路径绑定；失败才暂时跳过
        try:
            from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
            on_protect = is_protect_account_page(page) or is_ott_code_page(page)
        except Exception:
            on_protect = D.count(page, '#EmailAddress') > 0 or D.count(page, '#iOttText') > 0

        if on_protect and self.bind_recovery_email:
            if self._try_bind_recovery_email(page):
                self.log_event(
                    'REGISTER', 'OK', 'recovery_bind',
                    '注册阶段备用邮箱绑定成功（OAuth 通常不再出现此页）',
                )
                acted = True
            else:
                if self._click_if_visible(page, '#iShowSkip'):
                    self._mark_recovery_skipped()
                    self.log_event(
                        'REGISTER', 'WARN', 'skip_recovery',
                        '注册阶段绑定失败，已 #iShowSkip；OAuth 仍可能再要求绑定',
                    )
                    acted = True
        elif on_protect or D.count(page, '#iShowSkip') > 0:
            # temp_mail.enabled=false 时：只跳过
            if self._click_if_visible(page, '#iShowSkip'):
                self._mark_recovery_skipped()
                self.log_event('REGISTER', 'INFO', 'skip_recovery', '已点击 #iShowSkip 暂时跳过辅助邮箱')
                acted = True
            else:
                for text in ('暂时跳过', 'Skip for now', 'Skip'):
                    try:
                        if D.click_role_button(page, text) or D.click_if_visible(page, f'text:{text}'):
                            self._mark_recovery_skipped()
                            self.log_event('REGISTER', 'INFO', 'skip_recovery', f'已点击跳过: {text}')
                            acted = True
                            break
                    except Exception:
                        pass

        # 2) Windows 通行密钥 / Hello：优先点「取消」#idBtn_Back
        passkey_hint = False
        try:
            body = D.body_text(page, limit=1200)
            passkey_hint = any(
                k in body
                for k in (
                    '通行密钥', 'Windows Hello', 'passkey', 'Passkey',
                    '更快速地登录', 'face, fingerprint', 'security key',
                    '使用 Windows Hello', '创建通行密钥',
                )
            )
        except Exception:
            pass

        try:
            back = D.q(page, '#idBtn_Back')
            if back and back.states.is_displayed:
                value = ''
                try:
                    value = ((back.attr('value') or '') + ' ' + (back.text or '')).strip()
                except Exception:
                    value = ''
                is_cancel = any(k in value for k in ('取消', 'Cancel', 'No', 'not now', 'Not now', '暂时不要'))
                # 仅在确认是通行密钥/Hello 页时点取消；避免保护帐户流程里误点「取消」
                if passkey_hint and is_cancel:
                    if self._click_if_visible(page, '#idBtn_Back'):
                        self.log_event(
                            'REGISTER', 'INFO', 'skip_passkey',
                            f'已点击 #idBtn_Back value={value[:40]!r} passkey_hint={passkey_hint}',
                        )
                        acted = True
        except Exception:
            pass

        # 文本兜底
        if passkey_hint:
            for text in ('取消', 'Cancel', '暂时不要', 'Not now', 'Skip for now'):
                try:
                    if D.click_role_button(page, text):
                        self.log_event('REGISTER', 'INFO', 'skip_passkey', f'已点击按钮: {text}')
                        acted = True
                        break
                except Exception:
                    pass
                try:
                    if self._click_if_visible(page, f'input[type="button"][value="{text}"]'):
                        self.log_event('REGISTER', 'INFO', 'skip_passkey', f'已点击 input: {text}')
                        acted = True
                        break
                except Exception:
                    pass

        return acted

    def _enter_mailbox_after_register(self, page, timeout_ms=45000):
        """验证码通过后进入邮箱。

        保护帐户/绑定辅助邮箱为**概率事件**（日志 2026-07-19 多批验证）：
          - 可能在注册后立刻出现 → 出现则绑定
          - 可能完全不出现 → 直接 mail/0 + OAuth（正常）
          - 也可能仅在 OAuth 中出现 → OAuth 侧再绑
        不因「未出现」而长时间空等。
        """
        mail_url = 'https://outlook.live.com/mail/0/'
        deadline = time.time() + timeout_ms / 1000.0
        force_count = 0
        # 短等：给拦截页一点渲染时间；不出现则继续
        protect_probe_deadline = time.time() + 5.0
        saw_protect = False

        try:
            page.wait(1.2)
        except Exception:
            pass

        while time.time() < deadline:
            try:
                from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
                on_protect = is_protect_account_page(page) or is_ott_code_page(page)
            except Exception:
                on_protect = (
                    D.count(page, '#EmailAddress') > 0
                    or D.count(page, '#iOttText') > 0
                    or D.count(page, '#iShowSkip') > 0
                )
            if on_protect:
                if not saw_protect:
                    self.log_event(
                        'REGISTER', 'INFO', 'recovery',
                        '检测到保护帐户页（概率出现），开始绑定辅助邮箱',
                    )
                saw_protect = True

            if self._dismiss_post_register_intercepts(page):
                page.wait(0.8)
                continue

            if self._is_mailbox_url(page) and not on_protect:
                if D.count(page, '#iShowSkip') == 0 and D.count(page, '#EmailAddress') == 0:
                    st = self.recovery_bind_status()
                    self.log_event(
                        'REGISTER', 'OK', 'mail_enter',
                        f'已在邮箱页 recovery_bound={st["bound"]} skipped={st["skipped"]} '
                        f'saw_protect={saw_protect} url={page.url}',
                    )
                    return True

            # 短探针窗口：仅多等几秒看是否弹出保护页
            if self.bind_recovery_email and not saw_protect and time.time() < protect_probe_deadline:
                page.wait(0.4)
                continue

            if force_count < 2:
                force_count += 1
                try:
                    self.log_event(
                        'REGISTER', 'INFO', 'mail_goto',
                        f'跳转邮箱({force_count}) saw_protect={saw_protect} {mail_url}',
                    )
                    page.get(mail_url)
                    page.wait(1.2)
                    self._dismiss_post_register_intercepts(page)
                    page.wait(0.6)
                    if self._is_mailbox_url(page):
                        if not self._dismiss_post_register_intercepts(page):
                            if D.count(page, '#EmailAddress') == 0 and D.count(page, '#iShowSkip') == 0:
                                st = self.recovery_bind_status()
                                self.log_event(
                                    'REGISTER', 'OK', 'mail_enter',
                                    f'直达邮箱 recovery_bound={st["bound"]} skipped={st["skipped"]} saw_protect={saw_protect}',
                                )
                                return True
                except Exception as exc:
                    self.log_event('REGISTER', 'WARN', 'mail_goto', f'跳转邮箱失败: {exc}')
                    page.wait(0.8)
            else:
                page.wait(0.5)

        try:
            page.get(mail_url)
            self._dismiss_post_register_intercepts(page)
        except Exception:
            pass

        ok = self._is_mailbox_url(page)
        try:
            final_url = page.url
        except Exception:
            final_url = ''
        self.log_event(
            'REGISTER',
            'OK' if ok else 'WARN',
            'mail_enter',
            f'最终 url={final_url} ok={ok}',
        )
        return ok

    # ============================================================
    # 验证码入口
    # ============================================================
    def handle_captcha(self, page):
        """验证码入口。captcha_strategy: 0=全自动按压, 1=半自动(暂停等你手动按)"""
        if self.captcha_strategy == 1:
            return self._captcha_manual(page)
        return self._captcha_hold(page)

    def _hand_off_at_captcha(self, page, email, password):
        """策略 2：填表已到验证码界面，验证码 + 进邮箱 + OAuth 全部交人工。"""
        self.log_event(
            'REGISTER', 'WARN', 'handoff',
            f'已到验证码界面，交由人工完成：{email}{self.email_suffix} / {password}',
        )
        return 'handed_off'

    def _captcha_manual(self, page):
        """半自动模式：脚本暂停，你只需在浏览器窗口里手动过「人机验证」本身。
        验证码之后可能弹出的「保护你的帐户/绑定辅助邮箱」「设置通行密钥」等页面，
        由脚本复用全自动流程自动处理（你不用点），直到进入邮箱为止（最多 5 分钟）；
        之后拿 OAuth token 等「后面的活」照常全自动。"""
        self.log_event('CAPTCHA', 'WARN', 'manual',
                       '请在浏览器窗口手动完成人机验证；验证码之后的保护帐户/通行密钥等页面脚本会自动处理...')
        for _ in range(300):
            page.wait(1.0)
            try:
                # 你过完人机后可能先弹保护帐户/绑辅助邮箱/通行密钥——复用自动流程处理，你不用管
                self._dismiss_post_register_intercepts(page)
                if self._is_mailbox_url(page):
                    page.wait(2.0)
                    self.log_event('CAPTCHA', 'OK', 'manual', '已进入邮箱！')
                    return True
            except Exception:
                pass
        self.log_event('CAPTCHA', 'FAIL', 'manual', '超时（5分钟），未进入邮箱。')
        return False

    # ============================================================
    # 全自动按压验证码
    # ============================================================
    def _captcha_hold(self, page):
        """验证码分派：先定位挑战 iframe，再按类型解题。

        - PerimeterX「按住」(hsprotect / #px-captcha)：单次长按不放 + 微颤，等 checkmark / 挑战消失。
        - Arkose「点击」(circle / svg / 可访问性挑战)：点目标 → 等『再次按下』→ 点击。
        所有鼠标事件走 dp_page 原始 CDP，带 force(pressure)≈0.5，screenX/screenY 已被 STEALTH_JS 补丁修正。
        """
        frames = self._wait_for_captcha_frame(page)
        if not frames:
            self.bump_failure('captcha_btn2_never_appeared')
            self.penalize_ip(penalty=4)
            self._log("未检测到验证码iframe")
            return False

        frame1, frame2 = frames
        if self._is_px_challenge(page, frame1):
            if self.px_solve_mode == 'a11y':
                return self._solve_px_a11y(page, frame1)
            return self._solve_px_hold(page, frame1)
        return self._solve_arkose(page, frame1, frame2)

    def _solve_arkose(self, page, frame1, frame2):
        """Arkose 点击式挑战（旧版微软验证码）：点目标 → 等按钮2 → 点击 → 检查结果。"""
        self._human_prelude(page)
        btn2_seen = False

        _tries = max(1, self.max_captcha_retries)
        for attempt in range(_tries):
            self._log(f"Hold {attempt+1}/{_tries}")
            page.wait(random.randint(200, 600) / 1000)

            # 每轮重新解析 frame，防止刷新后句柄失效
            frame1, frame2 = self._resolve_captcha_frames(page) or (frame1, frame2)
            if not frame2:
                continue

            # ① 在iframe中找到可点击的目标元素
            box, target_label = self._find_target(frame2, attempt)
            if not box:
                continue

            cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
            # ② 选择按压位置（中心/边缘/角落/随机）
            pos_name, x, y = self._pick_position(box, cx, cy)
            self._log(f"target={target_label} pos={pos_name}")

            # ③ 从远处Bezier曲线移动到目标按钮（未按下，buttons=0）
            from_x, from_y = x + random.uniform(-250, 250), y + random.uniform(-250, 250)
            D.mouse_move(page, from_x, from_y, buttons=0)
            page.wait(random.randint(40, 150) / 1000)
            self._natural_move(page, from_x, from_y, x, y)

            # ④ C:double-tap — 双击→松开→长按（带 force）
            D.mouse_press(page, x, y); page.wait(random.randint(25, 55) / 1000)
            D.mouse_release(page, x, y); page.wait(random.randint(80, 220) / 1000)
            D.mouse_press(page, x, y); page.wait(random.randint(25, 55) / 1000)
            D.mouse_release(page, x, y); page.wait(random.randint(120, 380) / 1000)
            D.mouse_press(page, x, y)

            # ⑤ 按住并圆形微颤，等按钮2出现
            appeared = self._hold_and_wait(page, frame2, x, y)
            if not appeared:
                D.mouse_release(page, x, y)
                continue
            btn2_seen = True

            # ⑥ click或dblclick轻量偏置轮换
            bm = self._pick_b2mode()
            self._record_b2_attempt(bm)
            if not self._execute_b2(page, frame2, x, y, bm):
                continue

            # ⑦ 检查验证码是否通过
            success, retry = self._check_captcha_result(page, frame1, frame2)
            if not success:
                break
            if not retry:
                with self._state_lock:
                    OutlookController._attempts += 1
                    OutlookController._success += 1
                self._record_b2_success(bm)
                self._record_ip('win')
                self._print_stats()
                return True

        with self._state_lock:
            OutlookController._attempts += 1
        if btn2_seen:
            self.bump_failure('captcha_btn2_appeared_but_failed')
        else:
            self.bump_failure('captcha_btn2_never_appeared')
            self.penalize_ip(penalty=4)
        self._record_ip('loss')
        self._print_stats()
        return False

    # ============================================================
    # PerimeterX「按住」验证码（hsprotect.net / #px-captcha）
    # ============================================================
    def _is_px_challenge(self, page, frame1):
        """判定当前是否 PerimeterX 按住挑战：frame1 内有 #px-captcha 或 url 属 hsprotect/px。"""
        try:
            if frame1 and D.q(frame1, '#px-captcha', timeout=0):
                return True
            u = (getattr(frame1, 'url', '') or '')
            if any(k in u for k in ('hsprotect', 'px-captcha', 'perimeterx', 'px-cdn')):
                return True
        except Exception:
            pass
        return False

    def _px_press_box(self, page, frame1):
        """真实视口按压盒子（自算 OOPIF 偏移）。返回 {'cx','cy','w','h'} 或 None。

        关键坑：验证质询/hsprotect 是跨域 OOPIF，DrissionPage 的 rect.viewport_* 只给
        「帧内相对坐标」，不折算 iframe 偏移。必须用主页 iframe 的 getBoundingClientRect
        作偏移，再叠加 #px-captcha 的帧内坐标，才是 CDP 需要的主视口坐标。
        """
        # 主页里 验证质询 iframe 的视口位置（真视口坐标）
        try:
            off = page.run_js(
                'var f=document.querySelector(\'iframe[title="验证质询"]\');'
                'if(!f)return null;var r=f.getBoundingClientRect();'
                'return [r.x,r.y,r.width,r.height];')
        except Exception:
            off = None
        if not off:
            return None
        ox, oy, iw, ih = off
        # frame1 内 #px-captcha 的帧内相对坐标
        rel = None
        try:
            rel = frame1.run_js(
                'var c=document.querySelector("#px-captcha")||document.querySelector("[tabindex]");'
                'if(!c)return null;var r=c.getBoundingClientRect();'
                'return [r.x,r.y,r.width,r.height];') if frame1 else None
        except Exception:
            rel = None
        if rel and rel[2] and rel[2] > 30:
            cx = ox + rel[0] + rel[2] / 2.0
            cy = oy + rel[1] + rel[3] / 2.0
            w, h = rel[2], rel[3]
        else:
            # 回退：按整块挑战 iframe 中心
            cx, cy = ox + iw / 2.0, oy + ih / 2.0
            w, h = iw, ih
        return {'cx': float(cx), 'cy': float(cy), 'w': float(w), 'h': float(h)}

    def _px_probe(self, page, frame1):
        """轻量探测（避免阻塞微颤）：gone(挑战消失=通过) / success / holding。"""
        try:
            if D.count(page, 'iframe[title="验证质询"]') == 0:
                return 'gone'
        except Exception:
            pass
        try:
            if 'outlook.live.com' in (page.url or ''):
                return 'success'
        except Exception:
            pass
        return 'holding'

    def _px_hold_and_watch(self, page, frame1, cx, cy, max_hold_ms=22000):
        """按住并持续圆周微颤（buttons=1 + force 抖动），轻量轮询成功信号。

        要点：按住期间必须持续微颤、不能被重活（截图/get_frame）打断，否则 PX 视作松手/中断。
        """
        start = time.time()
        last = None
        next_check = 0.0
        while (time.time() - start) * 1000 < max_hold_ms:
            self._circular_tremor(page, cx, cy, duration_ms=300)
            elapsed = (time.time() - start) * 1000
            if elapsed >= next_check:
                next_check = elapsed + 650
                st = self._px_probe(page, frame1)
                if st != last:
                    self._log(f"[PX] {int(elapsed)}ms state={st}")
                    last = st
                if st in ('gone', 'success'):
                    return 'success'
                if st == 'retry':
                    return 'retry'
        return 'timeout'

    def _px_deep_probe(self, page, frame1, tag=''):
        """一次性深度诊断：坐标映射 + 内层 iframe display + 按钮 rect + 截图。"""
        try:
            shot = os.path.join(os.path.dirname(self.log_path), f'pxshot_{tag}.png')
            page.get_screenshot(path=os.path.dirname(shot), name=os.path.basename(shot), full_page=True)
            self._log(f"[PXdbg:{tag}] screenshot -> {shot}")
        except Exception as e:
            self._log(f"[PXdbg:{tag}] shot err {e}")
        try:
            j = page.run_js(
                'var f=document.querySelector(\'iframe[title="验证质询"]\');'
                'if(!f)return "no-f";var r=f.getBoundingClientRect();'
                'return JSON.stringify({x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)});')
            self._log(f"[PXdbg:{tag}] main-iframe rect={j}")
        except Exception as e:
            self._log(f"[PXdbg:{tag}] main err {e}")
        try:
            el = D.q(frame1, '#px-captcha', timeout=0)
            self._log(f"[PXdbg:{tag}] #px-captcha vbox={D.viewport_box(el)}")
            j = frame1.run_js(
                'var c=document.querySelector("#px-captcha");'
                'var f=document.querySelector("iframe");'
                'var cs=f?getComputedStyle(f):null;var rr=f?f.getBoundingClientRect():null;'
                'return JSON.stringify({capDisp:c?getComputedStyle(c).display:"?",'
                'ifrInlineDisp:f?(f.style.display||"(none-inline)"):"no-ifr",'
                'ifrCompDisp:cs?cs.display:"?",'
                'ifrRect:rr?[Math.round(rr.x),Math.round(rr.y),Math.round(rr.width),Math.round(rr.height)]:null});')
            self._log(f"[PXdbg:{tag}] frame1 inner={j}")
        except Exception as e:
            self._log(f"[PXdbg:{tag}] frame1 err {e}")
        try:
            f = self._resolve_captcha_frames(page)
            if f:
                _, frame2 = f
                j = frame2.run_js(
                    'var b=document.querySelector("[role=button]");if(!b)return "no-btn";'
                    'var r=b.getBoundingClientRect();var p=document.querySelector("p");'
                    'return JSON.stringify({rect:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)],'
                    'cls:b.className,txt:p?p.textContent:""});')
                self._log(f"[PXdbg:{tag}] frame2 btn={j}")
        except Exception as e:
            self._log(f"[PXdbg:{tag}] frame2 err {e}")

    def _solve_px_hold(self, page, frame1):
        """PerimeterX 按住主循环：接近 → 按下(force) → 长按微颤 → 松开 → 判定，失败重试。"""
        self._human_prelude(page)
        if self._px_dbg():
            self._px_deep_probe(page, frame1, tag='pre')
        _tries = max(1, self.max_captcha_retries)
        for attempt in range(_tries):
            self._log(f"PX Hold {attempt + 1}/{_tries}")
            frame1 = D.get_frame(page, 'iframe[title="验证质询"]', timeout=2) or frame1
            box = self._px_press_box(page, frame1)
            if not box:
                # 盒子取不到：可能挑战已过（iframe 消失）→ 补判
                if self._px_gone(page):
                    self._log("[PX] 挑战已消失（按压盒子取不到），判定通过")
                    return self._px_win()
                self._log("[PX] 未取到可按压盒子，等待重试")
                page.wait(random.uniform(0.8, 1.5))
                continue

            # 按压点：中心附近抖动（box 已是真实视口坐标 + 尺寸）
            cx = box['cx'] + random.uniform(-box['w'] * 0.18, box['w'] * 0.18)
            cy = box['cy'] + random.uniform(-box['h'] * 0.28, box['h'] * 0.28)
            self._log(f"[PX] press @ ({int(cx)},{int(cy)}) box={int(box['w'])}x{int(box['h'])}")

            # 从远处贝塞尔接近（未按下）
            fx, fy = cx + random.uniform(-220, 220), cy + random.uniform(-160, 160)
            D.mouse_move(page, fx, fy, buttons=0)
            page.wait(random.randint(40, 130) / 1000)
            self._natural_move(page, fx, fy, cx, cy)
            page.wait(random.randint(60, 180) / 1000)

            # 按下（带 force）→ 长按微颤 → 松开
            D.mouse_press(page, cx, cy)
            result = self._px_hold_and_watch(page, frame1, cx, cy)
            D.mouse_release(page, cx, cy)
            self._log(f"[PX] result={result}")
            if attempt == 0 and self._px_dbg():
                self._px_deep_probe(page, frame1, tag='post1')

            # 松手后补判：hold 可能刚好在边界完成，给页面时间跳转再看挑战是否消失
            if result != 'success':
                page.wait(random.uniform(1.0, 1.6))
                if self._px_gone(page):
                    self._log("[PX] 松手后挑战已消失，判定通过")
                    result = 'success'

            if result == 'success':
                page.wait(random.uniform(0.6, 1.2))
                return self._px_win()
            # timeout / retry：短暂停后再来一轮
            page.wait(random.uniform(0.9, 2.0))

        with self._state_lock:
            OutlookController._attempts += 1
        self.bump_failure('captcha_btn2_appeared_but_failed')
        self._record_ip('loss')
        self._print_stats()
        return False

    def _px_dbg(self):
        """是否开启 PX 深度诊断（截图/内部 dump）：设环境变量 PX_DEBUG=1 启用。"""
        return bool(os.environ.get('PX_DEBUG'))

    def _px_gone(self, page):
        """验证质询挑战是否已从主页消失（=按压通过后被移除）。"""
        try:
            return D.count(page, 'iframe[title="验证质询"]') == 0
        except Exception:
            return False

    def _px_win(self):
        """记一次验证码通过：更新统计并返回 True。"""
        with self._state_lock:
            OutlookController._attempts += 1
            OutlookController._success += 1
        self._record_ip('win')
        self._print_stats()
        return True

    # ============================================================
    # PerimeterX 无障碍(accessibility)备用解法（config: px_solve_mode='a11y'）
    #   点无障碍「小人」图标 → 等进度条自动走完 → 单击长条确认（无需持续按压）。
    #   ⚠ PX 内层 UI 为跨域 OOPIF 且类名混淆，下面的候选选择器按已知形态编写；
    #     首轮会把 frame2 可交互元素 dump 到日志，便于按真实 DOM 收敛选择器。
    # ============================================================
    def _frame2_offset(self, page, frame1):
        """内层 PX UI(frame2) 左上角相对主视口的偏移 (ox, oy)。

        坐标三级折算：主页 iframe[title=验证质询] 偏移 + frame1 内层 iframe 偏移，
        叠加 frame2 内元素自身 rect，才是 CDP Input 需要的主视口坐标。
        """
        try:
            off = page.run_js(
                'var f=document.querySelector(\'iframe[title="验证质询"]\');'
                'if(!f)return null;var r=f.getBoundingClientRect();return [r.x,r.y];')
        except Exception:
            off = None
        if not off:
            return None
        inner = None
        try:
            inner = frame1.run_js(
                'var f=document.querySelector(\'iframe[style*="display: block"]\')'
                '||document.querySelector("#px-captcha iframe")'
                '||document.querySelector("iframe");'
                'if(!f)return [0,0];var r=f.getBoundingClientRect();return [r.x,r.y];')
        except Exception:
            inner = None
        if not inner:
            inner = [0.0, 0.0]
        return float(off[0]) + float(inner[0]), float(off[1]) + float(inner[1])

    def _px_a11y_locate(self, page, frame1, frame2, selectors):
        """在 frame2 内按候选选择器找首个可见元素，返回 (sel, cx, cy, w, h) 主视口坐标或 None。"""
        offset = self._frame2_offset(page, frame1)
        if not offset:
            return None
        ox, oy = offset
        for sel in selectors:
            sel_lit = json.dumps(sel)
            try:
                rel = frame2.run_js(
                    'var e=document.querySelector(%s);if(!e)return null;'
                    'var r=e.getBoundingClientRect();'
                    'if(r.width<3||r.height<3)return null;'
                    'return [r.x,r.y,r.width,r.height];' % sel_lit)
            except Exception:
                rel = None
            if rel:
                return (sel, ox + rel[0] + rel[2] / 2.0, oy + rel[1] + rel[3] / 2.0,
                        rel[2], rel[3])
        return None

    def _px_a11y_dump(self, page, frame1, frame2):
        """全面 dump 无障碍图标候选：main + frame1(验证质询容器) + frame2(内层UI) 三层，
        列出所有有尺寸(6~320px)的元素(tag/id/class/aria/role/title/cursor/背景图/svg/onclick + box)，
        按 x 升序——按钮左侧、同一行、最左的小方块通常就是无障碍「小人」图标。
        用于把真实选择器/坐标钉死（首轮跑一次即可据此收敛 _px_a11y_find_icon）。"""
        js = (
            'var out=[];var els=document.querySelectorAll("*");'
            'for(var i=0;i<els.length;i++){var e=els[i];'
            'var r=e.getBoundingClientRect();'
            'if(r.width<6||r.height<6||r.width>320||r.height>220)continue;'
            'var cs=null;try{cs=getComputedStyle(e);}catch(x){}'
            'var bg=cs?cs.backgroundImage:"none";'
            'var svg=(e.tagName==="svg")||!!e.querySelector("svg,img");'
            'var c=(e.className&&e.className.baseVal!==undefined)?e.className.baseVal:e.className;'
            'out.push({t:e.tagName,id:(e.id||"").slice(0,16),'
            'cls:(c||"").toString().slice(0,24),'
            'role:e.getAttribute("role"),al:e.getAttribute("aria-label"),'
            'ti:e.getAttribute("title"),'
            'cur:(cs&&cs.cursor==="pointer")?1:0,'
            'bg:(bg&&bg!=="none")?1:0,svg:svg?1:0,'
            'clk:(e.onclick!=null||e.getAttribute("onclick")!=null)?1:0,'
            'x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)});'
            '}out.sort(function(a,b){return a.x-b.x;});'
            'return JSON.stringify(out.slice(0,60));')
        for name, fr in (('main', page), ('frame1', frame1), ('frame2', frame2)):
            if not fr:
                continue
            try:
                self._log(f"[PXa11y-dump] {name}={fr.run_js(js)}")
            except Exception as e:
                self._log(f"[PXa11y-dump] {name} err {e}")

    def _px_a11y_click_point(self, page, x, y):
        """CDP 单击某主视口坐标（贝塞尔接近 + 短按松开）。"""
        fx = x + random.uniform(-160, 160)
        fy = y + random.uniform(-120, 120)
        D.mouse_move(page, fx, fy, buttons=0)
        page.wait(random.randint(40, 120) / 1000)
        self._natural_move(page, fx, fy, x, y)
        page.wait(random.randint(60, 160) / 1000)
        D.mouse_press(page, x, y)
        page.wait(random.randint(45, 110) / 1000)
        D.mouse_release(page, x, y)

    def _px_a11y_shot(self, page, name):
        """PX_DEBUG 诊断截图（仅可视视口，不 resize/scroll，避免污染几何）。"""
        try:
            page.get_screenshot(path=os.path.dirname(self.log_path), name=name + '.png')
            self._log(f"[PXa11y-shot] {name}.png")
        except Exception as e:
            self._log(f"[PXa11y-shot] {name} err {e}")

    def _px_a11y_probe_geometry(self, page, frame1, frame2):
        """定位诊断（首轮跑一次）：记 #px-captcha 真实视口盒 + devicePixelRatio + 全页截图，
        并无过滤 dump frame2(内层)全部元素(含0×0)、探测是否还有更深 iframe(frame3)。
        截图 + 已知 #px-captcha 盒 → 可据像素精确量出无障碍图标相对挑战条的横向偏移。"""
        box = self._px_press_box(page, frame1)
        try:
            dpr = page.run_js('return window.devicePixelRatio;')
        except Exception:
            dpr = '?'
        self._log(f"[PXa11y-geo] px-captcha box={box} dpr={dpr}")
        alljs = (
            'var out=[];var els=document.querySelectorAll("*");'
            'for(var i=0;i<els.length&&i<60;i++){var e=els[i];var r=e.getBoundingClientRect();'
            'var c=(e.className&&e.className.baseVal!==undefined)?e.className.baseVal:e.className;'
            'out.push({t:e.tagName,role:e.getAttribute("role"),al:e.getAttribute("aria-label"),'
            'cls:(c||"").toString().slice(0,20),txt:(e.textContent||"").trim().slice(0,16),'
            'box:[Math.round(r.x),Math.round(r.y),Math.round(r.width),Math.round(r.height)]});}'
            'return JSON.stringify(out);')
        try:
            self._log(f"[PXa11y-geo] frame2-all={frame2.run_js(alljs)}")
        except Exception as e:
            self._log(f"[PXa11y-geo] frame2-all err {e}")
        try:
            f3 = D.get_frame(frame2, 'iframe', timeout=1)
            if f3:
                self._log(f"[PXa11y-geo] frame3-all={f3.run_js(alljs)}")
            else:
                self._log("[PXa11y-geo] frame2 无更深 iframe")
        except Exception as e:
            self._log(f"[PXa11y-geo] frame3 err {e}")
        try:
            shot_dir = os.path.dirname(self.log_path)
            # 只截可视视口：full_page=True 会 resize/scroll 视口，导致截图后 #px-captcha 盒
            # 位置被持久平移（实测 cy 442→538），污染随后 _px_a11y_find_icon 的几何定位。
            page.get_screenshot(path=shot_dir, name='pxa11y_probe.png')
            self._log(f"[PXa11y-geo] screenshot -> {os.path.join(shot_dir, 'pxa11y_probe.png')}")
            # 截图后再量一次盒子，确认视口未被截图动过（cy 应与截图前一致）
            b2 = self._px_press_box(page, frame1)
            if b2:
                self._log(f"[PXa11y-geo] box-after-shot cy={b2['cy']:.1f} (截图前应一致)")
        except Exception as e:
            self._log(f"[PXa11y-geo] shot err {e}")

    # 无障碍图标相对 #px-captcha 的横向位置比例（0=最左边缘, 0.5=中心）。
    # 据真机截图 + 已知 #px-captcha 盒(360×42, cx=761.6)量得：图标圆心 CSS-x≈643，
    # 即 (643-581.6)/360≈0.17；垂直居中(cy)。#px-captcha 是容器，图标+按钮居中其内、图标在左。
    _PXA11Y_ICON_XFRAC = 0.17

    def _px_layer_offsets(self, page, frame1):
        """三层 → 主视口坐标偏移：{'main':(0,0),'frame1':(ox,oy),'frame2':(ox2,oy2)}。
        frame1 偏移=验证质询 iframe 视口位置；frame2 偏移=再叠加内层 iframe 位置。"""
        offs = {'main': (0.0, 0.0)}
        try:
            f1 = page.run_js(
                'var f=document.querySelector(\'iframe[title="验证质询"]\');'
                'if(!f)return null;var r=f.getBoundingClientRect();return [r.x,r.y];')
        except Exception:
            f1 = None
        if not f1:
            return offs
        offs['frame1'] = (float(f1[0]), float(f1[1]))
        inner = None
        try:
            inner = frame1.run_js(
                'var f=document.querySelector(\'iframe[style*="display: block"]\')'
                '||document.querySelector("#px-captcha iframe")'
                '||document.querySelector("iframe");'
                'if(!f)return [0,0];var r=f.getBoundingClientRect();return [r.x,r.y];') if frame1 else [0, 0]
        except Exception:
            inner = [0, 0]
        if not inner:
            inner = [0, 0]
        offs['frame2'] = (float(f1[0]) + float(inner[0]), float(f1[1]) + float(inner[1]))
        return offs

    def _px_a11y_find_icon(self, page, frame1, frame2):
        """定位无障碍「小人」图标 → (desc, cx, cy) 主视口坐标或 None。

        实测（2026-07-26 真机 dump）：PX 内层 UI 是跨域 OOPIF，按钮/图标 getBoundingClientRect
        多为 0×0；main 层只有微软页 chrome（贪婪 svg 选择器会误命中 Microsoft logo）。唯一可靠锚点
        是 frame1 内 #px-captcha 的真实视口盒(360×42, _px_press_box 已验证)。故图标按「#px-captcha
        左端、同高」几何定位，横向比例 _PXA11Y_ICON_XFRAC 据截图量得。仅在 frame1/frame2 试少量
        无障碍语义选择器作为未来兼容，绝不搜 main。"""
        for lname, fr in (('frame1', frame1), ('frame2', frame2)):
            if not fr:
                continue
            offs = self._px_layer_offsets(page, frame1)
            if lname not in offs:
                continue
            ox, oy = offs[lname]
            for sel in ('[aria-label*="ccessib"]', '[aria-label*="无障碍"]',
                        '[title*="ccessib"]', '[aria-label*="udio"]', '[class*="a11y"]'):
                sel_lit = json.dumps(sel)
                try:
                    rel = fr.run_js(
                        'var e=document.querySelector(%s);if(!e)return null;'
                        'var r=e.getBoundingClientRect();'
                        'if(r.width<6||r.height<6)return null;'
                        'return [r.x,r.y,r.width,r.height];' % sel_lit)
                except Exception:
                    rel = None
                if rel:
                    return ("%s:%s" % (lname, sel), ox + rel[0] + rel[2] / 2.0,
                            oy + rel[1] + rel[3] / 2.0)
        # 主路径：#px-captcha 左端、同高的几何定位（真实视口坐标可靠）
        box = self._px_press_box(page, frame1)
        if box:
            ix = box['cx'] - box['w'] / 2.0 + box['w'] * self._PXA11Y_ICON_XFRAC
            return ('geom:px-left', ix, box['cy'])
        return None

    def _px_a11y_button_text(self, frame1, frame2):
        """读内层「按住/请稍候/再次按下」按钮当前文案；frame2 优先，失败回退 frame1。"""
        for fr in (frame2, frame1):
            if not fr:
                continue
            try:
                t = fr.run_js(
                    'var b=document.querySelector(\'[role="button"]\')'
                    '||document.querySelector("button");'
                    'return b?(b.textContent||"").trim():"";')
                if t:
                    return t
            except Exception:
                continue
        return ''

    def _px_a11y_screenshot_np(self, page):
        """截可视视口 → numpy RGB 数组(H,W,3)。优先 as_bytes 不落盘；失败回退临时文件。
        PIL/numpy 不可用或截图失败 → None（调用方据此退化为时间兜底）。"""
        try:
            from PIL import Image
            import numpy as np
            import io
        except Exception:
            return None
        data = None
        try:
            data = page.get_screenshot(as_bytes='png')
        except Exception:
            data = None
        if not data:
            try:
                d = os.path.dirname(self.log_path)
                page.get_screenshot(path=d, name='pxa11y_poll.png')
                with open(os.path.join(d, 'pxa11y_poll.png'), 'rb') as f:
                    data = f.read()
            except Exception:
                return None
        try:
            return np.array(Image.open(io.BytesIO(data)).convert('RGB'))
        except Exception:
            return None

    def _px_a11y_blue_frac(self, page, box, dpr):
        """量「按钮区」微软蓝占比。实测(真机截图)：按住≈0.04 / 请稍候(填充中)≈0.14 /
        再次按下(满格纯蓝#0F6CBD)≈0.80 / 消失≈0.00。box 为 #px-captcha 主视口 CSS 盒，
        截图为设备像素(=CSS×dpr)。图标在左端(~0.17)，按钮占右侧 0.26~0.98 宽。None=读取失败。"""
        im = self._px_a11y_screenshot_np(page)
        if im is None:
            return None
        try:
            import numpy as np
            H, W = im.shape[:2]
            cx, cy = box['cx'] * dpr, box['cy'] * dpr
            w, h = box['w'] * dpr, box['h'] * dpr
            x0 = max(0, int(cx - w / 2 + 0.26 * w))
            x1 = min(W, int(cx - w / 2 + 0.98 * w))
            y0 = max(0, int(cy - h / 2 * 0.7))
            y1 = min(H, int(cy + h / 2 * 0.7))
            if x1 <= x0 or y1 <= y0:
                return None
            c = im[y0:y1, x0:x1].astype(int)
            R, G, B = c[..., 0], c[..., 1], c[..., 2]
            m = (B > 110) & (B > R + 40) & (B > G + 25)
            return float(m.mean())
        except Exception:
            return None

    def _px_a11y_wait_progress(self, page, frame1, frame2, max_ms=26000, min_dwell=6.0):
        """点无障碍图标后等进度条自动走完。用像素法判定按钮是否已定格「再次按下」——
        DOM 文案恒为「按住 •••」不可用，可见的「请稍候/再次按下」及进度填充均为 canvas 绘制。
        判据：按钮区蓝占比 ≥0.70 且相邻三采样稳定(进度停止增长=定格) → 'press_again'。
        返回 'success'(挑战直接消失) / 'press_again'(满格待补击，含像素读失败时的时间兜底) / 'timeout'。
        进度期间 frame 可能刷新，故每轮重解析 frames 并早退 px_gone。"""
        box0 = self._px_press_box(page, frame1)
        try:
            dpr = float(page.run_js('return window.devicePixelRatio;') or 1.25)
        except Exception:
            dpr = 1.25
        start = time.time()
        prev = None
        stable_hi = 0
        while (time.time() - start) * 1000 < max_ms:
            if self._px_gone(page) or self._px_probe(page, frame1) in ('gone', 'success'):
                return 'success'
            fr = self._resolve_captcha_frames(page)
            if fr:
                frame1, frame2 = fr
            box = self._px_press_box(page, frame1) or box0
            bf = self._px_a11y_blue_frac(page, box, dpr) if box else None
            el = time.time() - start
            if bf is not None:
                hi = bf >= 0.70
                steady = prev is not None and abs(bf - prev) < 0.06
                stable_hi = (stable_hi + 1) if (hi and steady) else 0
                prev = bf
                if self._px_dbg():
                    self._log(f"[PXa11y] t={el:.1f}s 蓝={bf:.2f} stable={stable_hi}")
                if el >= min_dwell and stable_hi >= 2:
                    self._log(f"[PXa11y] 进度条已满(蓝={bf:.2f} t={el:.1f}s)→补击确认")
                    return 'press_again'
            page.wait(1.1)
        # 兜底：实测进度条 ≤~22s 必走完且「再次按下」态持续，像素读失败也补一击（优于放弃）
        self._log(f"[PXa11y] 像素未判定，按兜底({max_ms}ms)补击确认")
        return 'press_again'

    def _px_a11y_progress_probe(self, page, frame1, frame2, secs=22):
        """PX_DEBUG 诊断：点图标后每 2s 采样内层 DOM/CSS，找出反映「请稍候/再次按下」
        与进度条填充的真实信号（textContent 恒为「按住 •••」，可见文案另有来源）。"""
        js = r'''
          var out=[];
          function push(k,v){ if(v!=null) out.push(k+'='+String(v).slice(0,60)); }
          var b=document.querySelector('[role="button"]')||document.querySelector('button');
          if(b){
            push('tc',(b.textContent||'').trim());
            push('it',(b.innerText||'').trim());
            push('al',b.getAttribute('aria-label'));
            push('bef',getComputedStyle(b,'::before').content);
            push('aft',getComputedStyle(b,'::after').content);
          }
          var all=document.querySelectorAll('*');
          for(var i=0;i<all.length;i++){
            var e=all[i];
            var st=e.getAttribute&&e.getAttribute('style');
            if(st && /(width|transform|scale|clip)/i.test(st)) push('style<'+e.tagName+'.'+(e.className||'')+'>',st);
            var t=(e.textContent||'').trim();
            if(/请稍候|再次|按下|稍候|gain|wait/i.test(t)) push('txt<'+e.tagName+'.'+(e.className||'')+'>',t);
            try{ var cb=getComputedStyle(e,'::before').content; if(cb && cb!=='none' && cb!=='normal' && cb!=='""') push('bef<'+e.tagName+'>',cb);}catch(_){}
          }
          return JSON.stringify(out);
        '''
        start = time.time()
        while time.time() - start < secs:
            el = int((time.time() - start) * 1000)
            gone = self._px_gone(page)
            fr = self._resolve_captcha_frames(page)
            if fr:
                frame1, frame2 = fr
            info = None
            src = ''
            for name, f in (('f2', frame2), ('f1', frame1)):
                if not f:
                    continue
                try:
                    r = f.run_js(js)
                except Exception as ex:
                    r = 'err:' + str(ex)[:40]
                if r and r != '[]':
                    info, src = r, name
                    break
            self._log(f"[PXa11y-prog] t={el} gone={gone} {src}={info}")
            if gone:
                return
            page.wait(2.0)

    def _solve_px_a11y(self, page, frame1):
        """PerimeterX 无障碍解法（px_solve_mode='a11y'）。据真机截图确认的真实流程：
        单击按钮左侧的无障碍「小人」图标（点一下，不按住）→ 进度条自动走完 →
        按钮变「再次按下」→ 单击按钮确认 → 挑战消失即通过。
        找不到图标入口时退化为按住法(_solve_px_hold)，保证不比默认更差。
        总尝试次数同样受 max_captcha_retries 约束。
        """
        self._human_prelude(page)
        _tries = max(1, self.max_captcha_retries)
        for attempt in range(_tries):
            self._log(f"PX a11y {attempt + 1}/{_tries}")
            frames = self._resolve_captcha_frames(page)
            if not frames:
                if self._px_gone(page):
                    self._log("[PXa11y] 挑战已消失，判定通过")
                    return self._px_win()
                page.wait(random.uniform(0.8, 1.5))
                continue
            frame1, frame2 = frames

            if attempt == 0 and self._px_dbg():
                self._px_a11y_dump(page, frame1, frame2)
                self._px_a11y_probe_geometry(page, frame1, frame2)

            # ① 找并单击无障碍小人图标（点一下，不按住）
            icon = self._px_a11y_find_icon(page, frame1, frame2)
            if not icon:
                if attempt == 0:
                    self._log("[PXa11y] 未找到无障碍图标，退化为按住法")
                    return self._solve_px_hold(page, frame1)
                page.wait(random.uniform(0.9, 1.6))
                continue
            desc, ix, iy = icon
            self._log(f"[PXa11y] 单击无障碍图标 {desc} @ ({int(ix)},{int(iy)})")
            if attempt == 0 and self._px_dbg():
                self._px_a11y_shot(page, 'pxa11y_before_click')
            self._px_a11y_click_point(page, ix, iy)
            page.wait(random.uniform(0.5, 1.0))
            if attempt == 0 and self._px_dbg():
                self._px_a11y_shot(page, 'pxa11y_after_click')

            # ② 等进度条自动走完（像素法判定按钮变「再次按下」；DOM 文案不可用）
            prog = self._px_a11y_wait_progress(page, frame1, frame2)
            self._log(f"[PXa11y] 进度结果={prog}")
            if attempt == 0 and self._px_dbg():
                self._px_a11y_shot(page, 'pxa11y_after_progress')
            if prog == 'success' or self._px_gone(page):
                self._log("[PXa11y] 进度后挑战消失，判定通过")
                return self._px_win()

            # ③ 「再次按下」→ 单击 #px-captcha 中心确认（最多补两次，防首击落空）
            if prog == 'press_again':
                fr = self._resolve_captcha_frames(page)
                if fr:
                    frame1, frame2 = fr
                for ci in range(2):
                    box = self._px_press_box(page, frame1)
                    if not box:
                        break
                    self._log(f"[PXa11y] 点「再次按下」确认#{ci + 1} @ ({int(box['cx'])},{int(box['cy'])})")
                    self._px_a11y_click_point(page, box['cx'], box['cy'])
                    page.wait(random.uniform(1.0, 1.6))
                    if attempt == 0 and self._px_dbg():
                        self._px_a11y_shot(page, f'pxa11y_after_confirm{ci + 1}')
                    if self._px_gone(page) or self._px_probe(page, frame1) in ('gone', 'success'):
                        self._log("[PXa11y] 确认后挑战消失，判定通过")
                        return self._px_win()
                    fr = self._resolve_captcha_frames(page)
                    if fr:
                        frame1, frame2 = fr

            # ④ 判定
            if self._px_gone(page) or self._px_probe(page, frame1) in ('gone', 'success'):
                self._log("[PXa11y] 判定通过")
                return self._px_win()
            page.wait(random.uniform(0.8, 1.6))

        with self._state_lock:
            OutlookController._attempts += 1
        self.bump_failure('captcha_btn2_appeared_but_failed')
        self._record_ip('loss')
        self._print_stats()
        return False


    def _record_ip(self, result):
        """记录本次运行中IP的表现（仅内存，不持久化）。result: 'win' 或 'loss'"""
        proxy = getattr(self.thread_local, '_proxy', '')
        key = proxy.split('//')[-1] if '//' in proxy else proxy
        if key:
            with self._state_lock:
                if key not in self._ip_tracker:
                    self._ip_tracker[key] = {'win': 0, 'total': 0}
                self._ip_tracker[key]['total'] += 1
                if result == 'win':
                    self._ip_tracker[key]['win'] += 1

    def _print_stats(self):
        """打印当前累计的验证码通过率"""
        with self._state_lock:
            a = max(OutlookController._attempts, 1)
            s = OutlookController._success
            b2_attempts = dict(OutlookController._b2_attempts)
            b2_success = dict(OutlookController._b2_success)
        b2_fragments = []
        for mode in ('click', 'dblclick'):
            attempts = b2_attempts.get(mode, 0)
            if attempts <= 0:
                continue
            wins = b2_success.get(mode, 0)
            rate = wins / attempts * 100
            b2_fragments.append(f"{mode}:{wins}/{attempts}={rate:.0f}%")
        suffix = f" | b2={' '.join(b2_fragments)}" if b2_fragments else ""
        self._log(f"[Stats] {s}/{a}={s / a * 100:.0f}%{suffix}")

    # ============================================================
    # iframe / 人类化 / 鼠标移动
    # ============================================================
    def _resolve_captcha_frames(self, page):
        """解析嵌套验证码 iframe：外层 title=验证质询 → 内层可见 iframe。返回 (frame1, frame2) 或 None。"""
        frame1 = D.get_frame(page, 'iframe[title="验证质询"]', timeout=2)
        if not frame1:
            return None
        frame2 = D.get_frame(frame1, 'iframe[style*="display: block"]', timeout=2)
        if not frame2:
            # 兜底：取第一个内层 iframe（PX 的「人工验证挑战」/ Arkose 的可见挑战）
            frame2 = D.get_frame(frame1, 'iframe', timeout=2)
        if not frame2:
            return None
        return frame1, frame2

    def _wait_for_captcha_frame(self, page):
        """轮询等待验证码 iframe 就绪，最多约 20 秒。成功返回 (frame1, frame2)，失败返回 None。

        两类就绪判据：
        - PerimeterX：frame1 内 #px-captcha 可见（width>30）即可按住。
        - Arkose：frame2 内出现 circle/svg/可访问性挑战 等可点目标。
        """
        px_targets = ['#px-captcha', 'div[id][tabindex]']
        arkose_targets = ['[aria-label="可访问性挑战"]', 'circle', 'svg', '[role="button"]']
        for _ in range(20):
            try:
                frames = self._resolve_captcha_frames(page)
                if frames:
                    frame1, frame2 = frames
                    # PX：可见 #px-captcha
                    for sel in px_targets:
                        el = D.q(frame1, sel, timeout=0)
                        box = D.viewport_box(el) if el else None
                        if box and box['width'] > 30 and box['height'] > 8:
                            self._log(f"PX挑战就绪: {sel} {int(box['width'])}x{int(box['height'])}")
                            page.wait(random.randint(400, 1000) / 1000)
                            return frames
                    # Arkose：frame2 里的可点目标
                    for sel in arkose_targets:
                        try:
                            el = D.q(frame2, sel, timeout=0)
                            if el:
                                box = D.viewport_box(el)
                                if box and box['width'] > 5:
                                    self._log(f"Arkose挑战就绪: {sel}")
                                    page.wait(random.randint(500, 1500) / 1000)
                                    return frames
                        except Exception:
                            continue
            except Exception:
                pass
            page.wait(1.0)
        return None

    def _human_prelude(self, page):
        """验证码前的随机行为：滚动、游荡、停顿、手抖，模拟真人操作"""
        for _ in range(random.randint(1, 4)):
            act = random.random()
            if act < 0.3:
                try:
                    page.run_js(f'window.scrollBy(0, {random.randint(-200, 200)})')
                except Exception:
                    pass
                page.wait(random.randint(200, 800) / 1000)
            elif act < 0.5:
                D.mouse_move(page, random.randint(100, 600), random.randint(100, 500), buttons=0)
                page.wait(random.randint(300, 1200) / 1000)
            elif act < 0.75:
                page.wait(random.randint(500, 2500) / 1000)
            else:
                try:
                    px = 400 + random.random() * 100
                    py = 300 + random.random() * 100
                    D.mouse_move(page, px, py, buttons=0)
                except Exception:
                    pass
                page.wait(random.randint(100, 400) / 1000)

    def _natural_move(self, page, x1, y1, x2, y2):
        """三段式人类鼠标轨迹：阶段1 Bezier加速接近(70%步数) → 阶段2 随机过冲 → 阶段3 微调修正"""
        # 控制点随机偏移，确保每次轨迹都不同
        cpx = (x1 + x2) / 2 + random.uniform(-150, 150)
        cpy = (y1 + y2) / 2 + random.uniform(-120, 120)
        # 阶段1: 加速接近 (ease-out 减速)
        steps1 = random.randint(8, 18)
        for i in range(steps1 + 1):
            t = i / steps1
            ease = 1 - (1 - t) ** 3
            px = (1 - ease) * x1 + ease * x2
            py = (1 - ease) * y1 + ease * y2
            bx = (1 - t) ** 2 * x1 + 2 * (1 - t) * t * cpx + t ** 2 * x2
            py = (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cpy + t ** 2 * y2
            px = px * 0.6 + bx * 0.4  # 混合线性进度 + Bezier弯曲
            D.mouse_move(page, px, py, buttons=0)
            page.wait(random.randint(6, 18) / 1000)
        # 阶段2: 过冲 (超过目标再回来，模拟手没停稳)
        if random.random() < 0.6:
            D.mouse_move(page, x2 + random.uniform(2, 8) * random.choice([-1, 1]),
                         y2 + random.uniform(2, 6) * random.choice([-1, 1]), buttons=0)
            page.wait(random.randint(30, 80) / 1000)
        # 阶段3: 修正到精确位置
        D.mouse_move(page, x2, y2, buttons=0)
        page.wait(random.randint(20, 60) / 1000)

    # ============================================================
    # 目标定位 / 位置 / 按压 / 微颤 / 按钮2
    # ============================================================
    def _find_target(self, frame2, attempt):
        """在验证码iframe中遍历候选选择器，找到尺寸>8px的第一个可见目标"""
        for sel in ['[aria-label="可访问性挑战"]', 'circle', 'ellipse',
                    'svg circle', 'svg ellipse', '[role="button"]', 'svg']:
            try:
                candidates = D.q_all(frame2, sel)
                cnt = len(candidates)
                if cnt > 0:
                    idx = attempt % min(cnt, 3)
                    box = D.viewport_box(candidates[idx])
                    if box and box['width'] > 8 and box['height'] > 8:
                        return box, f"{sel}[{idx}/{cnt}]"
            except Exception:
                continue
        return None, ""

    def _pick_position(self, box, cx, cy):
        """在目标元素上随机选取按压点：中心12%、边缘18%、角落18%、随机偏移52%"""
        r = random.random()
        if r < 0.12:
            return "center", cx + random.uniform(-3, 3), cy + random.uniform(-3, 3)
        elif r < 0.30:
            e = random.choice(['t', 'b', 'l', 'r'])
            if e == 't':   return f"edge.{e}", cx + random.uniform(-box['width']*0.3, box['width']*0.3), box['y'] + random.uniform(1, 5)
            elif e == 'b': return f"edge.{e}", cx + random.uniform(-box['width']*0.3, box['width']*0.3), box['y']+box['height'] - random.uniform(1, 5)
            elif e == 'l': return f"edge.{e}", box['x'] + random.uniform(1, 5), cy + random.uniform(-box['height']*0.3, box['height']*0.3)
            else:          return f"edge.{e}", box['x']+box['width'] - random.uniform(1, 5), cy + random.uniform(-box['height']*0.3, box['height']*0.3)
        elif r < 0.48:
            c = random.choice(['tl', 'tr', 'bl', 'br'])
            if c == 'tl':   return f"corner.{c}", box['x'] + random.uniform(2, 8), box['y'] + random.uniform(2, 8)
            elif c == 'tr': return f"corner.{c}", box['x']+box['width'] - random.uniform(2, 8), box['y'] + random.uniform(2, 8)
            elif c == 'bl': return f"corner.{c}", box['x'] + random.uniform(2, 8), box['y']+box['height'] - random.uniform(2, 8)
            else:           return f"corner.{c}", box['x']+box['width'] - random.uniform(2, 8), box['y']+box['height'] - random.uniform(2, 8)
        else:
            return "random", cx + random.uniform(-box['width']*0.4, box['width']*0.4), cy + random.uniform(-box['height']*0.4, box['height']*0.4)

    def _hold_and_wait(self, page, frame2, x, y):
        """按住状态下圆形微颤，等待"再次按下"按钮出现。出现后延续按压1.5-4.5s"""
        self._circular_tremor(page, x, y, duration_ms=random.randint(600, 1800))
        appeared = False
        btn2_selectors = ['[aria-label="再次按下"]', '[aria-label*="再次"]', '[aria-label*="按下"]']
        # 边微颤边探测 btn2，最多约 10s（保持按住，不松手）
        for _ in range(20):
            for sel in btn2_selectors:
                if D.q(frame2, sel, timeout=0):
                    appeared = True
                    break
            if appeared:
                break
            self._circular_tremor(page, x, y, duration_ms=500)
        if appeared:
            extra_ms = random.randint(1500, 4500)
            self._log(f"btn2出现, 延续{extra_ms}ms")
            self._circular_tremor(page, x, y, duration_ms=extra_ms)
        return appeared

    def _circular_tremor(self, page, x, y, duration_ms):
        """按住期间的圆周微颤（buttons=1 + force 抖动），模拟手指自然颤抖与压力变化"""
        steps = max(duration_ms // 50, 5)
        radius = random.uniform(0.3, 2.0)
        for i in range(steps):
            angle = 2 * math.pi * i / steps + random.uniform(-0.3, 0.3)
            tx = x + math.cos(angle) * radius * random.uniform(0.7, 1.3)
            ty = y + math.sin(angle) * radius * random.uniform(0.7, 1.3)
            D.mouse_move(page, tx, ty, buttons=1, force=round(random.uniform(0.45, 0.55), 3))
            page.wait(random.randint(35, 70) / 1000)

    def _pick_b2mode(self):
        """轻量延续旧版策略：保留探索，但优先当前运行中表现更好的btn2模式。"""
        with self._state_lock:
            attempts = dict(OutlookController._b2_attempts)
            wins = dict(OutlookController._b2_success)
        weights = {}
        for mode in ('click', 'dblclick'):
            attempted = attempts.get(mode, 0)
            success = wins.get(mode, 0)
            if attempted >= 10:
                rate = success / max(attempted, 1)
                weights[mode] = rate ** 2 * 10 if rate >= 0.30 else max(0.05, rate)
            elif attempted >= 5:
                weights[mode] = max(0.1, success / max(attempted, 1))
            else:
                weights[mode] = 1.0
        return random.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]

    def _record_b2_attempt(self, mode):
        with self._state_lock:
            OutlookController._b2_attempts[mode] = OutlookController._b2_attempts.get(mode, 0) + 1

    def _record_b2_success(self, mode):
        with self._state_lock:
            OutlookController._b2_success[mode] = OutlookController._b2_success.get(mode, 0) + 1

    def _execute_b2(self, page, frame2, x, y, bm):
        """操作按钮2：定位 → 移动 → click或dblclick（带 force 的 CDP 指针）"""
        page.wait(random.randint(300, 900) / 1000)
        btn2_selectors = ['[aria-label="再次按下"]', '[aria-label*="再次"]', '[aria-label*="按下"]']
        btn2_box = None
        for sel in btn2_selectors:
            try:
                el = D.q(frame2, sel, timeout=0)
                if el:
                    btn2_box = D.viewport_box(el)
                    if btn2_box:
                        break
            except Exception:
                continue
        if not btn2_box:
            return False
        # 在按钮2上随机偏移点击位置
        b2cx, b2cy = btn2_box['x']+btn2_box['width']/2, btn2_box['y']+btn2_box['height']/2
        x2 = b2cx + random.uniform(-btn2_box['width']*0.35, btn2_box['width']*0.35)
        y2 = b2cy + random.uniform(-btn2_box['height']*0.35, btn2_box['height']*0.35)
        D.mouse_move(page, x2, y2, buttons=0)
        page.wait(random.randint(50, 180) / 1000)

        def _click(cx, cy):
            D.mouse_press(page, cx, cy)
            page.wait(random.randint(30, 70) / 1000)
            D.mouse_release(page, cx, cy)

        if bm == "dblclick":
            _click(x2, y2)
            page.wait(random.randint(80, 200) / 1000)
            _click(x2 + random.uniform(-3, 3), y2 + random.uniform(-3, 3))
        else:
            _click(x2, y2)
        return True

    def _check_captcha_result(self, page, frame1, frame2):
        """检测验证码结果。返回 (success, retry):
        - (True, False): 通过
        - (True, True): 需重试
        - (False, False): 失败/IP被封
        """
        try:
            if not D.wait_gone(page, '.draw', timeout=15):  # 等待加载动画消失
                raise TimeoutError('.draw not detached')
            loading = D.q(page, '[role="status"][aria-label="正在加载..."]', timeout=5)
            if loading:
                page.wait(8.0)
                if D.count(page, 'text:一些异常活动') or D.count(page, 'text:此站点正在维护') > 0:
                    return False, False  # IP被风控
                if D.count(frame2, '[aria-label="可访问性挑战"]') > 0:
                    return True, True    # 验证码重置，需要重试
                return True, False        # 验证码通过
            else:
                if D.count(page, 'text:取消') > 0:
                    return True, False    # 取消按钮出现 → 通过
                if D.q(frame1, 'text:请再试一次', timeout=15):  # 提示重试
                    return True, True
                return False, False       # 无加载/无重试提示/无取消 → 失败（对齐原逻辑）
        except Exception:
            if D.count(page, 'text:取消') > 0:
                return True, False
            return False, False           # .draw未消失 → 失败
