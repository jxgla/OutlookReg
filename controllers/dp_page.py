"""DrissionPage 兼容层 / 反检测辅助。

集中封装所有 DrissionPage 专有用法，让 oauth2 / recovery_bind / outlook_controller
的改动尽量是「Playwright 用法 → 这里的门面函数」的一一替换。

三块内容：
1. 浏览器构建与每个 tab 的反检测准备（build_browser / prepare_tab）。
2. 反检测向量修复：
   - CDP `Input.dispatchMouseEvent` 的 screenX/screenY == clientX/clientY 泄漏补丁
     （press-and-hold 验证码头号修复）。
   - 带 `force`(pressure) 的原始 CDP 指针事件（真实鼠标 pressure≈0.5，CDP 默认 0）。
   - locale / timezone / geolocation 用 CDP Emulation 覆盖（DP 无 context 级选项）。
3. 查询门面：q / q_all / count / vis / click_sel / fill_sel / text_* / role_button …
   统一把 Playwright 风格 CSS 选择器翻译成 DP 定位符。
"""
import random
import time
from urllib.parse import parse_qs, urlparse

from DrissionPage import Chromium, ChromiumOptions

# DrissionPage 已识别的定位符前缀：命中则原样透传，否则按 CSS 处理
_DP_PREFIXES = ('css:', 'xpath:', 'x:', 'text:', 'text=', 'tag:', 't:', '@', '#', '.')

# 在每个新文档 document-start 注入：修 screenX/screenY 泄漏 + 补 webdriver 兜底
STEALTH_JS = r"""
(() => {
  try {
    // 真实鼠标事件 screenX/screenY 与 clientX/clientY 存在窗口偏移；
    // CDP Input.dispatchMouseEvent 派发时二者相等 -> 反爬据此判定 bot。
    const offX = () => {
      try { return (window.screenX || 0) + Math.max(0, (window.outerWidth || 0) - (window.innerWidth || 0)); }
      catch (e) { return 0; }
    };
    const offY = () => {
      try {
        const chrome = Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0));
        return (window.screenY || 0) + (chrome || 74);
      } catch (e) { return 74; }
    };
    for (const proto of [window.MouseEvent && MouseEvent.prototype,
                         window.PointerEvent && PointerEvent.prototype]) {
      if (!proto) continue;
      const dsx = Object.getOwnPropertyDescriptor(proto, 'screenX');
      const dsy = Object.getOwnPropertyDescriptor(proto, 'screenY');
      if (dsx && dsx.get) {
        Object.defineProperty(proto, 'screenX', {
          configurable: true, enumerable: true,
          get() { try { return Math.round(this.clientX + offX()); } catch (e) { return dsx.get.call(this); } },
        });
      }
      if (dsy && dsy.get) {
        Object.defineProperty(proto, 'screenY', {
          configurable: true, enumerable: true,
          get() { try { return Math.round(this.clientY + offY()); } catch (e) { return dsy.get.call(this); } },
        });
      }
    }
  } catch (e) {}
  try {
    // 兜底：确保 navigator.webdriver 为 false（DP 一般已处理）
    if (navigator.webdriver) {
      Object.defineProperty(navigator, 'webdriver', { get: () => false, configurable: true });
    }
  } catch (e) {}
})();
"""

# 关闭页面输入框自动填充提示（与旧 _disable_auth_page_autofill 等价）
_DISABLE_AUTOFILL_JS = r"""
() => {
    document.querySelectorAll('input').forEach((el) => {
        try {
            el.setAttribute('autocomplete', 'off');
            el.setAttribute('autocapitalize', 'off');
            el.setAttribute('autocorrect', 'off');
            el.setAttribute('spellcheck', 'false');
            el.setAttribute('data-lpignore', 'true');
        } catch (e) {}
    });
}
"""


# ============================================================
# 浏览器构建 / tab 准备
# ============================================================
def build_options(proxy_url=None, headless=False, window_size=None, browser_path=None,
                  extra_args=None):
    """构造 ChromiumOptions。auto_port：多线程安全、自动空闲端口 + 临时用户目录。"""
    co = ChromiumOptions().auto_port()
    if browser_path:
        co.set_browser_path(browser_path)
    # 反检测核心：真实 Chrome + 去掉 --no-sandbox/--disable-gpu 等强自动化信号
    base_args = [
        '--disable-blink-features=AutomationControlled',
        '--lang=zh-CN',
        '--accept-lang=zh-CN,zh,en-US,en',
        '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
        '--disable-sync',
        '--disable-signin-promo',
        '--disable-one-click-signin',
        '--disable-account-consistency',
        '--disable-features=IdentityDiscAccountMenu,SignInProfileCreation,SigninIntercept,ImproveSigninUI,ChromeSigninPromo,ChromeSigninFlow,Sync,ExplicitBrowserSignin,AccountConsistency,ExtensionsToolbarMenu,AutofillServerCommunication,PasswordManagerOnboarding,PasswordImport,Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,CalculateNativeWinOcclusion',
        '--disable-extensions',
        '--disable-default-apps',
        '--no-first-run',
        '--no-default-browser-check',
        '--disable-save-password-bubble',
        '--disable-component-update',
        '--disable-background-timer-throttling',
        '--disable-backgrounding-occluded-windows',
        '--disable-renderer-backgrounding',
    ]
    if window_size:
        base_args.append(f'--window-size={window_size[0]},{window_size[1]}')
    for a in base_args:
        co.set_argument(a)
    for a in (extra_args or []):
        co.set_argument(a)
    if proxy_url:
        # 直接下 --proxy-server，绕过 DrissionPage set_proxy 对 socks/账密代理的无关警告
        # （pool 模式传本地「无认证 socks5」转发器口，DP 会误报「不支持socks代理」）。
        # 这里手动复刻 set_proxy 的效果：记录 _proxy + 加参数，但不打印那条提醒。
        co._proxy = proxy_url
        co.set_argument('--proxy-server', proxy_url)
    if headless:
        co.headless(True)
    return co


def build_browser(proxy_url=None, headless=False, window_size=None, browser_path=None,
                  extra_args=None):
    """启动浏览器，返回 (browser, tab)。tab 已完成反检测准备。"""
    co = build_options(proxy_url, headless, window_size, browser_path, extra_args)
    browser = Chromium(co)
    tab = browser.latest_tab
    return browser, tab


def apply_emulation(tab, timezone=None, locale=None, loc=None):
    """CDP 覆盖时区 / 语言 / 地理位置（Playwright context 选项的 DP 等价物）。"""
    if timezone:
        try:
            tab.run_cdp('Emulation.setTimezoneOverride', timezoneId=timezone)
        except Exception:
            pass
    if locale:
        try:
            tab.run_cdp('Emulation.setLocaleOverride', locale=locale)
        except Exception:
            pass
    if loc:
        try:
            lat, lng = str(loc).split(',')
            tab.run_cdp(
                'Emulation.setGeolocationOverride',
                latitude=float(lat), longitude=float(lng), accuracy=random.uniform(20, 80),
            )
        except Exception:
            pass


def inject_stealth(tab):
    """在每个新文档最早期注入 screenX 补丁等反检测脚本。"""
    try:
        tab.run_cdp('Page.addScriptToEvaluateOnNewDocument', source=STEALTH_JS)
    except Exception:
        pass


def enable_webauthn_autoskip(tab, attempts=4):
    """安装 CDP 虚拟认证器，静默吞掉 passkey 的 WebAuthn 调用。

    微软注册/登录中途常弹「设置通行密钥」，点「设置/下一步」会触发
    navigator.credentials.create() → **Windows Hello 系统级弹窗**（浏览器自动化
    无法操作该 OS 模态框，流程直接卡死）。装一个 internal 平台虚拟认证器后：
    create/get 由虚拟认证器处理并立即成功（automaticPresenceSimulation + isUserVerified），
    **永不弹系统框**；即便页面自动调用 WebAuthn 也不阻塞。
    平台认证器「存在」也更像真机，不损隐身。每个 tab 建好后调用一次。

    **并发坑（已修）**：多窗口几乎同时启动时，某些 tab 的 CDP 会话尚未就绪，
    首个 WebAuthn.enable/addVirtualAuthenticator 可能抛错被吞 → 那个窗口没装上认证器 →
    passkey 页仍触发真实 Windows Hello 卡死（表现为「只有第一个窗口能跳过」）。
    故改为重试若干次 + 每次失败短等，确保每个并发窗口都装上。返回是否成功。
    """
    last_err = None
    for i in range(max(1, attempts)):
        try:
            try:
                tab.run_cdp('WebAuthn.enable', enableUI=False)
            except Exception:
                tab.run_cdp('WebAuthn.enable')
            tab.run_cdp('WebAuthn.addVirtualAuthenticator', options={
                'protocol': 'ctap2',
                'transport': 'internal',
                'hasResidentKey': True,
                'hasUserVerification': True,
                'isUserVerified': True,
                'automaticPresenceSimulation': True,
            })
            return True
        except Exception as e:
            last_err = e
            try:
                tab.wait(0.35)
            except Exception:
                time.sleep(0.35)
    return False


def minimize_window(tab):
    """把浏览器窗口最小化，让它挂后台静默跑、不抢占前台/焦点。

    仅隐藏窗口，**不改变 headful 本质**（仍是真实 Chrome，反检测能力不变）；
    配合 build_options 里的反节流 + 关闭 occlusion 计算，最小化后 JS/渲染/CDP 仍全速，
    验证码按压、计时器均不受影响。失败静默忽略（如 headless 无窗口时）。
    """
    try:
        info = tab.run_cdp('Browser.getWindowForTarget')
        wid = info['windowId']
        tab.run_cdp('Browser.setWindowBounds', windowId=wid,
                    bounds={'windowState': 'minimized'})
        return True
    except Exception:
        return False


def prepare_tab(tab, timezone=None, locale=None, loc=None, load_mode='normal'):
    """新 tab 统一准备：反检测注入 + emulation + 加载模式 + 超时。

    load_mode 默认 normal：eager 会导致嵌套 iframe 子文档元素查询失败
    （验证码 frame2 内元素定位不到）。
    """
    inject_stealth(tab)
    enable_webauthn_autoskip(tab)
    apply_emulation(tab, timezone=timezone, locale=locale, loc=loc)
    try:
        if load_mode == 'eager':
            tab.set.load_mode.eager()
        elif load_mode == 'none':
            tab.set.load_mode.none()
        else:
            tab.set.load_mode.normal()
    except Exception:
        pass
    try:
        tab.set.timeouts(base=8, page_load=45, script=30)
    except Exception:
        pass
    return tab


def disable_autofill(tab):
    try:
        tab.run_js(_DISABLE_AUTOFILL_JS)
    except Exception:
        pass


# ============================================================
# 选择器翻译 + 查询门面（ctx = tab 或 frame，二者都有 ele/eles）
# ============================================================
def loc(sel):
    """Playwright 风格 CSS -> DP 定位符。已带 DP 前缀则原样返回。"""
    if not sel:
        return sel
    if sel.startswith(_DP_PREFIXES):
        return sel
    return 'css:' + sel


def q(ctx, sel, timeout=0):
    """查一个元素，找不到返回 None（不抛异常）。"""
    try:
        el = ctx.ele(loc(sel), timeout=timeout)
    except Exception:
        return None
    return el if el else None


def q_all(ctx, sel, timeout=0):
    """查全部元素，返回列表（可能为空）。"""
    try:
        return list(ctx.eles(loc(sel), timeout=timeout))
    except Exception:
        return []


def count(ctx, sel):
    return len(q_all(ctx, sel))


def _displayed(el):
    try:
        return bool(el) and el.states.is_displayed
    except Exception:
        # 拿到元素但判可见性异常时，保守视为存在即可见
        return bool(el)


def vis(ctx, sel, timeout=0):
    """首个匹配元素是否可见（等价旧 _locator_visible）。"""
    el = q(ctx, sel, timeout=timeout)
    return _displayed(el)


def click_sel(ctx, sel, timeout=5, by_js=False):
    """等待并点击首个匹配元素，返回是否成功。"""
    el = q(ctx, sel, timeout=timeout)
    if not el:
        return False
    try:
        el.click(by_js=by_js, timeout=max(timeout, 1.5))
        return True
    except Exception:
        try:
            el.click(by_js=True)
            return True
        except Exception:
            return False


def click_if_visible(ctx, sel, timeout=0):
    """仅当可见才点击（等价旧 _click_if_visible）。"""
    el = q(ctx, sel, timeout=timeout)
    if not _displayed(el):
        return False
    try:
        el.click(timeout=2.5)
        return True
    except Exception:
        try:
            el.click(by_js=True)
            return True
        except Exception:
            return False


def fill_sel(ctx, sel, text, timeout=5, clear=True):
    """填充输入框（真实输入，clear 先清空）。返回是否成功。"""
    el = q(ctx, sel, timeout=timeout)
    if not el:
        return False
    try:
        el.input(text, clear=clear)
        return True
    except Exception:
        return False


# --- 文本 / 角色 ---
def text_ele(ctx, text, timeout=0):
    """按文本包含查找元素。"""
    return q(ctx, f'text:{text}', timeout=timeout)


def text_exists(ctx, text):
    return bool(text_ele(ctx, text))


def role_button(ctx, name, timeout=0):
    """近似 get_by_role('button', name=...)：button / input[button|submit] / [role=button]。"""
    xp = (
        f'xpath://button[contains(normalize-space(.), "{name}")]'
        f' | //input[(@type="button" or @type="submit") and contains(@value, "{name}")]'
        f' | //*[@role="button" and contains(normalize-space(.), "{name}")]'
    )
    return q(ctx, xp, timeout=timeout)


def click_role_button(ctx, name, timeout=0):
    el = role_button(ctx, name, timeout=timeout)
    if not _displayed(el):
        return False
    try:
        el.click(timeout=3)
        return True
    except Exception:
        try:
            el.click(by_js=True)
            return True
        except Exception:
            return False


def body_text(ctx, limit=1200):
    """读取 body 文本摘要（等价旧 page.locator('body').inner_text()）。"""
    try:
        el = ctx.ele('css:body', timeout=0.6)
        if el:
            return (el.text or '')[:limit]
    except Exception:
        pass
    try:
        return (ctx.run_js('return document.body ? document.body.innerText : ""') or '')[:limit]
    except Exception:
        return ''


def wait_gone(ctx, sel, timeout=15):
    """等待元素从 DOM 消失（等价 wait_for(state='detached')）。"""
    try:
        return bool(ctx.wait.ele_deleted(loc(sel), timeout=timeout))
    except Exception:
        return False


def get_frame(ctx, sel, timeout=2):
    try:
        fr = ctx.get_frame(loc(sel), timeout=timeout)
        return fr if fr else None
    except Exception:
        return None


# ============================================================
# 原始 CDP 指针事件（带 force / pressure + 抖动）
# ============================================================
def _force():
    return round(random.uniform(0.45, 0.55), 3)


def mouse_move(tab, x, y, buttons=0, force=0.0):
    try:
        tab.run_cdp(
            'Input.dispatchMouseEvent', type='mouseMoved',
            x=float(x), y=float(y), button='none', buttons=int(buttons),
            force=float(force), pointerType='mouse',
        )
    except Exception:
        pass


def mouse_press(tab, x, y, force=None):
    try:
        tab.run_cdp(
            'Input.dispatchMouseEvent', type='mousePressed',
            x=float(x), y=float(y), button='left', buttons=1, clickCount=1,
            force=float(_force() if force is None else force), pointerType='mouse',
        )
    except Exception:
        pass


def mouse_release(tab, x, y):
    try:
        tab.run_cdp(
            'Input.dispatchMouseEvent', type='mouseReleased',
            x=float(x), y=float(y), button='left', buttons=0, clickCount=1,
            force=0.0, pointerType='mouse',
        )
    except Exception:
        pass


def viewport_mid(el):
    """元素中心相对主视口坐标（正是 CDP Input.dispatchMouseEvent 所需）。"""
    try:
        p = el.rect.viewport_midpoint
        if p:
            return float(p[0]), float(p[1])
    except Exception:
        pass
    return None


def viewport_box(el):
    """返回 {x,y,width,height}（视口坐标），近似 Playwright bounding_box。"""
    try:
        vloc = el.rect.viewport_location
        size = el.rect.size
        if vloc and size:
            return {'x': float(vloc[0]), 'y': float(vloc[1]),
                    'width': float(size[0]), 'height': float(size[1])}
    except Exception:
        pass
    return None


# ============================================================
# OAuth 回调 code 捕获
# ============================================================
def extract_code_from_url(url):
    if not url or 'localhost' not in url or 'code=' not in url:
        return None
    try:
        parsed = urlparse(url)
        return parse_qs(parsed.query).get('code', [None])[0]
    except Exception:
        return None


def start_code_listen(tab):
    """在导航到 authorize / 点同意前调用，监听跳转到 localhost 的回调包。"""
    try:
        tab.listen.start('localhost')
    except Exception:
        pass


def poll_code(tab, captured, timeout_ms=180000, poll_ms=250):
    """轮询 URL / JS href / 监听器，直到抓到 OAuth code。captured=[None] 复用旧签名。"""
    def _scan():
        if captured and captured[0]:
            return captured[0]
        for getter in (lambda: tab.url,
                       lambda: tab.run_js('return location.href')):
            try:
                code = extract_code_from_url(getter())
            except Exception:
                code = None
            if code:
                if captured is not None:
                    captured[0] = code
                return code
        # 监听器里排队的回调包
        try:
            pkt = tab.listen.wait(timeout=0.05, raise_err=False)
            if pkt:
                code = extract_code_from_url(getattr(pkt, 'url', ''))
                if code:
                    if captured is not None:
                        captured[0] = code
                    return code
        except Exception:
            pass
        return None

    got = _scan()
    if got:
        return got
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        got = _scan()
        if got:
            return got
        try:
            tab.wait(poll_ms / 1000.0)
        except Exception:
            time.sleep(poll_ms / 1000.0)
    return None
