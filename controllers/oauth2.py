import time
from urllib.parse import parse_qs, quote, urlparse

import requests

from DrissionPage.common import Keys

from controllers import dp_page as D

# === OAuth2 常量（默认值，可被 config['oauth2'] 覆盖，见 configure_oauth2）===
CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REDIRECT_URI = "https://localhost"
AUTH_SCOPE = "https://graph.microsoft.com/.default offline_access"
TOKEN_SCOPE = AUTH_SCOPE
RT_SCOPE = "full"
AUTHORIZE_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


def configure_oauth2(cfg):
    """用 config['oauth2'] 覆盖模块级 OAuth 常量（client_id / redirect_url / Scopes / tenant）。

    启动时调用一次。返回实际生效的 {client_id, redirect_uri, auth_scope, token_scope, rt_scope, tenant}，供上层（如结果写入）同步。
    授权 URL 使用 auth_scope；拿 refresh_token 的 /token 兑换使用 token_scope，可按 rt_scope 从全量授权范围中切到 Graph 或 IMAP/SMTP。

    tenant：授权/换 token 端点的租户段。默认 `common`，可同时覆盖个人与组织类 Microsoft 帐户；
    若你只希望个人账号，也可改成 `consumers`。其余可选 `organizations` / 具体 tenant id。
    """
    global CLIENT_ID, REDIRECT_URI, AUTH_SCOPE, TOKEN_SCOPE, RT_SCOPE, AUTHORIZE_URL, TOKEN_URL
    cfg = cfg or {}
    cid = str(cfg.get('client_id') or '').strip()
    if cid:
        CLIENT_ID = cid
    ru = str(cfg.get('redirect_url') or cfg.get('redirect_uri') or '').strip()
    if ru:
        REDIRECT_URI = ru
    scopes = cfg.get('Scopes') or cfg.get('scopes')
    auth_scope = AUTH_SCOPE
    if isinstance(scopes, (list, tuple)):
        joined = ' '.join(str(s).strip() for s in scopes if str(s).strip())
        if joined:
            auth_scope = joined
    elif isinstance(scopes, str) and scopes.strip():
        auth_scope = scopes.strip()
    AUTH_SCOPE = auth_scope

    graph_scopes = []
    imap_scopes = []
    common_scopes = []
    for item in AUTH_SCOPE.split():
        low = item.lower()
        if 'graph.microsoft.com/' in low:
            graph_scopes.append(item)
        elif 'outlook.office.com/' in low:
            imap_scopes.append(item)
        else:
            common_scopes.append(item)

    rt_scope = str(cfg.get('rt_scope') or cfg.get('RTScope') or cfg.get('rtScope') or 'graph').strip().lower()
    if rt_scope not in ('graph', 'imap'):
        rt_scope = 'graph'
    RT_SCOPE = rt_scope

    if rt_scope == 'imap':
        selected = imap_scopes
    else:
        selected = graph_scopes
    token_parts = common_scopes + selected
    TOKEN_SCOPE = ' '.join(part for part in token_parts if part).strip() or AUTH_SCOPE

    tenant = str(cfg.get('tenant') or cfg.get('authority') or '').strip().strip('/')
    if tenant:
        AUTHORIZE_URL = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
        TOKEN_URL = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    return {
        'client_id': CLIENT_ID,
        'redirect_uri': REDIRECT_URI,
        'auth_scope': AUTH_SCOPE,
        'token_scope': TOKEN_SCOPE,
        'rt_scope': RT_SCOPE,
        'tenant': tenant or 'common',
        'authorize_url': AUTHORIZE_URL,
    }

CONSENT_SELECTOR = '[data-testid="appConsentPrimaryButton"]'
EMAIL_SELECTOR = "#i0116"
EMAIL_NEXT_SELECTOR = "#idSIButton9"
PRIMARY_SELECTOR = '[data-testid="primaryButton"],input[data-testid="primaryButton"],input[type="submit"]'
# 个人帐户 HRD 选择（勿点工作/学校帐户）
MSA_TILE_SELECTOR = "#msaTile"
MSA_TILE_TITLE_SELECTOR = "#msaTileTitle"
PASSWORD_BYPASS_TEXTS = [
    "使用密码",
    "使用密码登录",
    "Use password instead",
    "Use your password",
    "Sign in with a password",
]
PASSWORD_WRONG_TEXTS = [
    "此密码不是你的 Microsoft 帐户的正确密码",
    "This password is incorrect",
    "你的帐户或密码不正确",
    "帐户或密码不正确",
    "账户或密码不正确",
    "Your account or password is incorrect",
    "incorrect account or password",
]
PASSWORD_BLOCKED_TEXTS = [
    "密码登录不可用",
    "请尝试其他方法",
    "Password login is not available",
    "Try another way",
    "Try a different way",
    "Sign-in method isn't available",
]
ACCOUNT_TYPE_HINT_TEXTS = [
    "哪种类型的帐户",
    "哪种类型的账户",
    "which type of account",
    "Work or school account",
    "工作或学校帐户",
    "工作或学校账户",
    "个人帐户",
    "个人账户",
    "Personal account",
]
LOGIN_EMAIL_HINT_TEXTS = [
    "登录",
    "sign in",
    "使用你的 microsoft 帐户",
    "use your microsoft account",
    "电子邮件地址、电话或 skype",
    "电子邮件地址或电话号码",
    "电子邮件、电话或 skype",
    "email, phone, or skype",
    "email address, phone, or skype",
    "输入电子邮件",
    "enter email",
]
AUTH_NAV_TIMEOUT_MS = 45000
AUTH_ENTRY_TIMEOUT_MS = 45000


def build_auth_url(prefer_sso=True):
    """构造授权 URL。

    prefer_sso=True（默认，COOKIE 路径）：
      - 不加 sso_reload，尽量用注册会话静默登录直接到 consent
    prefer_sso=False（NEW 冷启动）：
      - 可加 prompt=login 强制账密（一般仍不建议；默认也不加）
    """
    params = {
        'client_id': CLIENT_ID,
        'response_type': 'code',
        'redirect_uri': REDIRECT_URI,
        'scope': AUTH_SCOPE,
    }
    # 历史问题：sso_reload=true 会强制打断 cookie SSO，COOKIE 路径几乎必掉 #i0116
    if not prefer_sso:
        params['sso_reload'] = 'true'
    return f"{AUTHORIZE_URL}?{'&'.join(f'{k}={quote(v)}' for k, v in params.items())}"


def _extract_code_from_url(url):
    return D.extract_code_from_url(url)


def _wait_for_code_capture(page, captured_code, timeout_ms=180000, poll_ms=250):
    return D.poll_code(page, captured_code, timeout_ms=timeout_ms, poll_ms=poll_ms)


def _compact_exc(exc, max_len=180):
    """压缩 Playwright 异常，去掉多行 Call log，保持单行日志。"""
    text = str(exc) if exc is not None else ""
    if not text:
        return ""
    # 只保留第一行语义（如 Locator.click: Timeout 5000ms exceeded.）
    first = text.strip().splitlines()[0].strip()
    # 去掉 Call log 及之后整段
    for marker in ("Call log:", "\nCall log"):
        idx = text.find(marker)
        if idx >= 0:
            first = text[:idx].strip().splitlines()[0].strip()
            break
    if len(first) > max_len:
        first = first[: max_len - 3] + "..."
    return first


def _wait_for_auth_state_or_code(page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, poll_ms=500, ignore_states=None):
    ignore_states = set(ignore_states or ())
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if captured_code and _wait_for_code_capture(page, captured_code, timeout_ms=0):
            return 'code'
        state = _current_auth_entry_state(page)
        if state != 'unknown' and state not in ignore_states:
            return state
        page.wait(poll_ms/1000)
    if captured_code and _wait_for_code_capture(page, captured_code, timeout_ms=0):
        return 'code'
    state = _current_auth_entry_state(page)
    if state != 'unknown' and state not in ignore_states:
        return state
    return 'unknown'


def _vis(page, sel):
    """选择器首个匹配是否可见。"""
    return D.vis(page, sel)


def _text_exists(page, text):
    return D.text_exists(page, text)


def _password_input(page):
    """密码框：中英 accessible name + 常见 id。返回元素或 None。"""
    for name in ("密码", "Password", "password"):
        el = D.q(page, f'xpath://input[@type="password" and (@aria-label="{name}" or @placeholder="{name}" or @name="{name}")]')
        if el and D._displayed(el):
            return el
    for sel in ("#passwordEntry", "#i0118", 'input[type="password"]'):
        el = D.q(page, sel)
        if el and D._displayed(el):
            return el
    return None


def _is_account_type_page(page):
    """个人/工作帐户选择页（HRD splitter）。"""
    if _vis(page, MSA_TILE_SELECTOR):
        return True
    if _vis(page, MSA_TILE_TITLE_SELECTOR):
        return True
    # 文案兜底：同时出现个人 + 工作/学校 更稳
    has_personal = (
        _text_exists(page, "个人帐户")
        or _text_exists(page, "个人账户")
        or _text_exists(page, "Personal account")
    )
    has_work = (
        _text_exists(page, "工作或学校帐户")
        or _text_exists(page, "工作或学校账户")
        or _text_exists(page, "Work or school account")
    )
    if has_personal and has_work:
        return True
    for t in ACCOUNT_TYPE_HINT_TEXTS:
        if "哪种类型" in t or "which type" in t.lower():
            if _text_exists(page, t):
                return True
    return False


def _is_protect_account_page(page):
    """「让我们来保护你的帐户」备用邮箱页。"""
    try:
        from controllers.recovery_bind import is_protect_account_page, is_ott_code_page
        return is_protect_account_page(page) or is_ott_code_page(page)
    except Exception:
        if _vis(page, "#EmailAddress"):
            return True
        if _vis(page, "#iOttText"):
            return True
        return _text_exists(page, "保护你的帐户") or _text_exists(page, "保护您的帐户")


def _is_proof_verify_page(page):
    """冷登录：验证已绑定辅助邮箱 / 6 格验证码（不含仅 KMSI）。"""
    try:
        from controllers.recovery_bind import is_proof_confirm_page, is_code_entry_page
        return is_proof_confirm_page(page) or is_code_entry_page(page)
    except Exception:
        if _vis(page, "#proof-confirmation-email-input"):
            return True
        if _vis(page, "#codeEntry-0"):
            return True
        return _text_exists(page, "验证你的电子邮件") or _text_exists(page, "输入你的代码")


def _is_kmsi_only_page(page):
    try:
        from controllers.recovery_bind import is_kmsi_page, is_proof_confirm_page, is_code_entry_page
        return is_kmsi_page(page) and not is_proof_confirm_page(page) and not is_code_entry_page(page)
    except Exception:
        return _text_exists(page, "保持登录") or _text_exists(page, "Stay signed in")


def _is_login_email_page_loose(page):
    """宽松识别 OAuth 入口邮箱页：有时 #i0116 尚未稳定或状态机短暂给 unknown。"""
    if _vis(page, EMAIL_SELECTOR):
        return True
    try:
        body = (D.body_text(page, limit=900) or "").strip()
    except Exception:
        body = ""
    body_l = body.lower()
    hint = any(t in body for t in LOGIN_EMAIL_HINT_TEXTS if any('一' <= c <= '鿿' for c in t))
    hint = hint or any(t in body_l for t in LOGIN_EMAIL_HINT_TEXTS if not any('一' <= c <= '鿿' for c in t))
    if not hint:
        return False
    if _vis(page, EMAIL_NEXT_SELECTOR):
        return True
    try:
        btn = D.role_button(page, '下一步', timeout=0)
        if btn and D._displayed(btn):
            return True
    except Exception:
        pass
    return _text_exists(page, '下一步') or _text_exists(page, 'Next')


def _current_auth_entry_state(page):
    """登录页状态机（可见 DOM 锚点，固定优先级）。

    consent > account_type > protect_account > proof_verify > kmsi > login_email > login_password > unknown
    """
    if _vis(page, CONSENT_SELECTOR):
        return 'consent'
    if _is_account_type_page(page):
        return 'account_type'
    if _is_protect_account_page(page):
        return 'protect_account'
    if _is_proof_verify_page(page):
        return 'proof_verify'
    if _is_kmsi_only_page(page):
        return 'kmsi'
    if _is_login_email_page_loose(page):
        return 'login_email'
    if _password_input(page) is not None:
        return 'login_password'
    return 'unknown'


def _handle_kmsi(page, log):
    """保持登录状态？→ 点「否」secondaryButton。"""
    try:
        from controllers.recovery_bind import is_kmsi_page, _click_kmsi_no
        if is_kmsi_page(page):
            if _click_kmsi_no(page, log=log):
                log('kmsi', '已点保持登录「否」', 'OK')
            else:
                log('kmsi', '点击「否」失败', 'WARN')
            page.wait(0.8)
    except Exception as exc:
        log('kmsi', f'处理异常: {_compact_exc(exc)}', 'WARN')
    return _current_auth_entry_state(page)


def _handle_proof_verify(page, log, temp_mail_cfg=None, recovery_session=None, failure_hook=None):
    """OAuth 冷登录：验证已绑定辅助邮箱（发码 → #codeEntry-0..5 自动提交 → KMSI 否）。"""
    # 仅 KMSI 时不需要 jwt
    if _is_kmsi_only_page(page):
        return _handle_kmsi(page, log)

    log('proof_verify', '检测到「验证电子邮件/输入代码」页', 'WARN')
    session = recovery_session
    if not session or not session.get('address') or not session.get('jwt'):
        log('proof_verify', '无注册阶段保存的辅助邮箱 jwt，无法接码', 'FAIL')
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        return _current_auth_entry_state(page)
    try:
        from controllers.recovery_bind import verify_bound_email_on_login
        ok = verify_bound_email_on_login(
            page, session, temp_mail_cfg or {}, log=log,
        )
    except Exception as exc:
        log('proof_verify', f'验证异常: {_compact_exc(exc)}', 'FAIL')
        ok = False
    if ok:
        log('proof_verify', '辅助邮箱验证流程完成', 'OK')
    else:
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        log('proof_verify', '辅助邮箱验证失败', 'FAIL')
    page.wait(0.5)
    return _current_auth_entry_state(page)


def _handle_protect_account(page, log, temp_mail_cfg=None, failure_hook=None, already_bound=False, current_email_local=''):
    """OAuth 中的保护帐户页。

    whether 注册阶段是否已绑过，只作上下文日志；只要当前页真在保护帐户流，就先按当前 DOM 尝试绑定，
    失败后才允许走 skip 兜底，避免把 OAuth 真正要求再次验证/绑定的页面直接跳掉。
    """
    if not _is_protect_account_page(page):
        return _current_auth_entry_state(page)

    if already_bound:
        log('protect_account', '注册阶段已有 recovery 记录，但当前 OAuth 页仍要求处理，先按当前页继续绑定/验证', 'WARN')
    else:
        log('protect_account', 'OAuth 出现保护帐户页（概率事件），尝试绑定', 'WARN')

    cfg = temp_mail_cfg or {}
    ok = False
    if cfg.get('enabled', True):
        try:
            from controllers.recovery_bind import bind_recovery_email
            result = bind_recovery_email(page, cfg, log=log, local_name=(current_email_local or None))
            if isinstance(result, tuple):
                ok = bool(result[0])
            else:
                ok = bool(result)
        except Exception as exc:
            log('protect_account', f'绑定异常: {_compact_exc(exc)}', 'FAIL')
            ok = False

    if ok:
        log('protect_account', 'OAuth 阶段备用邮箱绑定成功', 'OK')
        page.wait(0.8)
    else:
        if failure_hook:
            try:
                failure_hook('recovery_bind_fail')
            except Exception:
                pass
        try:
            if _vis(page, '#iShowSkip'):
                D.click_sel(page, '#iShowSkip', timeout=4.0)
                log('protect_account', 'OAuth 绑定失败，已 #iShowSkip', 'WARN')
                page.wait(0.8)
        except Exception as exc:
            log('protect_account', f'跳过失败: {_compact_exc(exc)}', 'WARN')
    page.wait(0.5)
    st = _current_auth_entry_state(page)
    if ok and st == 'protect_account':
        try:
            if not _vis(page, '#EmailAddress') and not _vis(page, '#iOttText'):
                return 'unknown'
        except Exception:
            pass
    return st


def _dump_auth_page(page, log, stage='auth_dump'):
    """失败时记录 URL + 正文摘要，便于对照截图。"""
    try:
        url = page.url or ''
    except Exception:
        url = ''
    body = ''
    try:
        body = D.body_text(page, limit=240).replace('\n', ' ')
    except Exception:
        body = ''
    state = _current_auth_entry_state(page)
    log(stage, f"state={state} url={url[:180]} body={body!r}", 'WARN')
    return state


def _click_personal_account(page, log=None):
    """点 HRD「个人帐户」#msaTile（禁止点工作/学校）。"""
    clicked = False
    # 1) 标准 msa tile
    try:
        if _vis(page, MSA_TILE_SELECTOR):
            clicked = D.click_sel(page, MSA_TILE_SELECTOR, timeout=5)
            if clicked and log:
                log('account_type', '已点击 #msaTile 个人帐户', 'OK')
    except Exception as exc:
        if log:
            log('account_type', f'#msaTile 点击失败: {exc}', 'WARN')

    # 2) 标题区域
    if not clicked:
        try:
            if _vis(page, MSA_TILE_TITLE_SELECTOR):
                clicked = D.click_sel(page, MSA_TILE_TITLE_SELECTOR, timeout=5)
                if clicked and log:
                    log('account_type', '已点击 #msaTileTitle', 'OK')
        except Exception:
            pass

    # 3) 文案 role=button / 文本
    if not clicked:
        for text in ("个人帐户", "个人账户", "Personal account"):
            try:
                if D.click_role_button(page, text, timeout=1):
                    clicked = True
                    if log:
                        log('account_type', f'已点击 button:{text}', 'OK')
                    break
            except Exception:
                pass
            try:
                # 避免点到「重命名你的个人 Microsoft 帐户」链接：优先含 display 的 tile
                if D.click_if_visible(page, f'text:{text}'):
                    clicked = True
                    if log:
                        log('account_type', f'已点击 text:{text}', 'OK')
                    break
            except Exception:
                pass

    if clicked:
        try:
            page.wait(1.2)
        except Exception:
            pass
    elif log:
        log('account_type', '未找到可点击的个人帐户入口', 'WARN')
    return clicked


def _resolve_account_type(page, log, captured_code=None, max_rounds=3):
    """若在帐户类型页，点击个人帐户并返回新状态。"""
    state = _current_auth_entry_state(page)
    for _ in range(max_rounds):
        if state != 'account_type':
            return state
        log('account_type', '检测到个人/工作帐户选择页，点击个人帐户', 'WARN')
        if not _click_personal_account(page, log):
            _dump_auth_page(page, log, 'account_type_dump')
            return 'account_type'
        try:
            _settle_auth_page(page, log, 'account_type')
        except Exception:
            page.wait(0.8)
        state = _wait_for_auth_state_or_code(
            page,
            captured_code,
            timeout_ms=15000,
            ignore_states=set(),
        )
        # 点完仍可能短暂 unknown
        if state == 'unknown':
            state = _current_auth_entry_state(page)
    return state


def _wait_for_auth_entry_state(page, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, poll_ms=500, ignore_states=None):
    return _wait_for_auth_state_or_code(page, None, timeout_ms=timeout_ms, poll_ms=poll_ms, ignore_states=ignore_states)


def _settle_auth_page(page, log, stage, timeout_ms=AUTH_NAV_TIMEOUT_MS):
    try:
        page.wait.doc_loaded(timeout=timeout_ms / 1000)
    except Exception as e:
        log(stage, f'等待 doc_loaded 超时，继续检测入口: {e}', 'WARN')
    page.wait(1.2)


def _disable_auth_page_autofill(page, log=None):
    D.disable_autofill(page)
    if log:
        log('autofill', '已尝试关闭页面输入框自动填充提示', 'INFO')


def _submit_email_fill(page, full_email):
    _disable_auth_page_autofill(page)
    el = page.ele('css:' + EMAIL_SELECTOR, timeout=5)
    el.click()
    page.actions.type(Keys.ESCAPE)
    page.wait(0.15)
    el.clear()
    page.wait(0.1)
    el.input(full_email, clear=True)
    page.wait(0.3)
    page.actions.type(Keys.ESCAPE)
    page.wait(0.2)
    D.click_sel(page, EMAIL_NEXT_SELECTOR, timeout=5)


def _submit_email_type(page, full_email):
    _disable_auth_page_autofill(page)
    el = page.ele('css:' + EMAIL_SELECTOR, timeout=5)
    el.click()
    el.clear()
    page.wait(0.1)
    # 真实逐键输入（比 JS 注入更像人）
    page.actions.click(el).type(full_email)
    page.wait(0.25)
    page.actions.type(Keys.ESCAPE)
    page.wait(0.2)
    D.click_sel(page, EMAIL_NEXT_SELECTOR, timeout=5)


def _submit_email_js_exact(page, full_email):
    # 保留名字以兼容方法表；改为真实输入（不再走原生 setter 注入）
    el = page.ele('css:' + EMAIL_SELECTOR, timeout=10)
    _disable_auth_page_autofill(page)
    el.click()
    el.input(full_email, clear=True)
    page.wait(0.5)
    page.actions.type(Keys.ESCAPE)
    page.wait(0.2)
    D.click_sel(page, EMAIL_NEXT_SELECTOR, timeout=5)


def _submit_email(page, full_email, log):
    if not D.q(page, EMAIL_SELECTOR, timeout=10):
        raise RuntimeError("邮箱框未出现")
    methods = [
        ("fill", _submit_email_fill),
        ("type", _submit_email_type),
        ("js_exact", _submit_email_js_exact),
        ("js_exact_retry", _submit_email_js_exact),
    ]
    success_states = ('login_password', 'consent', 'code', 'account_type', 'protect_account')
    last_error = None
    last_stage = 'unknown'
    for name, method in methods:
        try:
            # 提交过程中可能已跳到帐户类型/密码/保护帐户
            cur = _current_auth_entry_state(page)
            if cur in success_states:
                if cur == 'account_type':
                    cur = _resolve_account_type(page, log)
                log('oauth_email', f"提交前已在阶段={cur}", 'OK')
                return cur
            if D.count(page, EMAIL_SELECTOR) == 0:
                cur = _current_auth_entry_state(page)
                if cur == 'account_type':
                    cur = _resolve_account_type(page, log)
                return cur
            cur_el = D.q(page, EMAIL_SELECTOR)
            current = (cur_el.property('value') if cur_el else '') or ''
            log('oauth_email', f"尝试 {name}，提交前值={current.strip()!r}", 'INFO')
            method(page, full_email)
            stage = _wait_for_auth_entry_state(page, timeout_ms=12000)
            last_stage = stage
            if stage == 'account_type':
                stage = _resolve_account_type(page, log)
                last_stage = stage
            if stage in ('login_password', 'consent', 'code', 'protect_account'):
                log('oauth_email', f"{name} 成功进入阶段={stage}", 'OK')
                return stage
            still_here = _vis(page, EMAIL_SELECTOR)
            err = ""
            if still_here:
                err_el = D.q(page, "#usernameError")
                err = (err_el.text if err_el else "") or ""
            log('oauth_email', f"{name} 后仍未进入下一阶段 stage={stage} error={err!r}", 'WARN')
        except Exception as exc:
            last_error = exc
            brief = _compact_exc(exc)
            log('oauth_email', f"{name} 失败: {brief}", 'WARN')
            # type 时常见：Next 点击超时但页面已导航到密码/帐户类型/同意页
            stage = _current_auth_entry_state(page)
            if stage == 'account_type':
                stage = _resolve_account_type(page, log)
            last_stage = stage
            if stage in ('login_password', 'consent', 'code', 'protect_account'):
                log('oauth_email', f"{name} 异常后已在阶段={stage}", 'OK')
                return stage
    if last_stage in ('login_password', 'consent', 'code', 'account_type', 'protect_account'):
        if last_stage == 'account_type':
            last_stage = _resolve_account_type(page, log)
        return last_stage
    if last_error:
        raise RuntimeError(f"邮箱提交失败: {_compact_exc(last_error)}")
    raise RuntimeError("邮箱提交后未进入密码页")


def _click_use_password(page):
    for text in PASSWORD_BYPASS_TEXTS:
        try:
            if D.click_role_button(page, text, timeout=0):
                page.wait(1.5)
                return
        except Exception:
            pass
        try:
            if D.click_if_visible(page, f'text:{text}'):
                page.wait(1.5)
                return
        except Exception:
            pass


def _describe_password_candidates(page):
    parts = []
    for selector in ('#passwordEntry', '#i0118', 'input[type="password"]'):
        try:
            items = D.q_all(page, selector)
            count = len(items)
            rows = []
            for idx, item in enumerate(items):
                try:
                    visible = item.states.is_displayed
                except Exception as exc:
                    visible = f"err:{exc.__class__.__name__}"
                try:
                    meta = item.run_js(
                        """function() {
                            const el = this;
                            return {
                                id: el.id || '',
                                name: el.name || '',
                                type: el.getAttribute('type') || '',
                                tabindex: el.getAttribute('tabindex') || '',
                                ariaHidden: el.getAttribute('aria-hidden') || '',
                                readonly: el.hasAttribute('readonly'),
                                disabled: !!el.disabled
                            };
                        }"""
                    )
                except Exception:
                    meta = {}
                rows.append(f"{idx}:visible={visible},meta={meta}")
            parts.append(f"{selector} count={count} [{' ; '.join(rows)}]")
        except Exception:
            parts.append(f"{selector} error")
    return " | ".join(parts)


def _password_locator(page, log, timeout_ms=15000):
    deadline = time.time() + timeout_ms / 1000
    last_snapshot = ""
    while time.time() < deadline:
        _click_use_password(page)
        for selector in ('#passwordEntry', '#i0118', 'input[type="password"]'):
            for idx, item in enumerate(D.q_all(page, selector)):
                try:
                    if item.states.is_displayed:
                        log('oauth_password', f"使用密码框 {selector}[{idx}]", 'INFO')
                        return item, f"{selector}[{idx}]"
                except Exception:
                    continue
        last_snapshot = _describe_password_candidates(page)
        page.wait(0.3)
    raise RuntimeError(f"未找到可见密码框：{last_snapshot}")


def _submit_password(page, password, log):
    _click_use_password(page)
    _disable_auth_page_autofill(page)
    log('oauth_password', f"密码候选快照：{_describe_password_candidates(page)}", 'INFO')
    locator, locator_name = _password_locator(page, log=log, timeout_ms=15000)
    # 清 readonly/aria-hidden 后走真实输入（不再原生 setter 注入）
    try:
        locator.run_js(
            """function() {
                const el = this;
                el.removeAttribute('readonly');
                el.removeAttribute('aria-hidden');
                el.style.opacity = '1';
                el.style.pointerEvents = 'auto';
            }"""
        )
    except Exception:
        pass
    try:
        locator.click()
        locator.input(password, clear=True)
    except Exception:
        try:
            page.actions.click(locator).type(password)
        except Exception:
            pass
    page.wait(0.2)
    try:
        filled_len = len(locator.property('value') or '')
        log('oauth_password', f"{locator_name} 已写入密码，长度={filled_len}", 'INFO')
    except Exception:
        log('oauth_password', f"{locator_name} 已写入密码", 'INFO')
    page.wait(0.4)
    try:
        btn = D.q(page, '[data-testid="primaryButton"]', timeout=5)
        if btn:
            btn.click()
            log('oauth_password', "点击 data-testid=primaryButton 提交密码", 'INFO')
        else:
            raise RuntimeError('no primaryButton')
    except Exception:
        try:
            page.actions.type(Keys.ENTER)
        except Exception:
            pass
        log('oauth_password', "主按钮点击失败，改用 Enter 提交密码", 'WARN')


def _has_invalid_password(page):
    for t in PASSWORD_WRONG_TEXTS:
        if _text_exists(page, t):
            return True
    return False


def _has_password_login_blocked(page):
    for t in PASSWORD_BLOCKED_TEXTS:
        if _text_exists(page, t):
            return True
    return False


def _has_unknown_account(page):
    return (
        _text_exists(page, '找不到使用该用户名的帐户')
        or _text_exists(page, '找不到使用该用户名的账户')
        or _text_exists(page, "We couldn't find an account with that username")
        or _text_exists(page, "That Microsoft account doesn't exist")
        or _vis(page, '#usernameError')
    )


def _dismiss_passkey_setup(page, log=None):
    """密码后可能跳到「正在设置密钥」/ fido create，尝试取消回到同意流。"""
    try:
        url = page.url or ''
    except Exception:
        url = ''
    body_hint = False
    try:
        body_hint = (
            _text_exists(page, '正在设置密钥')
            or _text_exists(page, '安全窗口')
            or _text_exists(page, 'passkey')
            or _text_exists(page, '通行密钥')
            or 'fido/create' in url
        )
    except Exception:
        pass
    if not body_hint and 'fido' not in url:
        return False
    if log:
        log('passkey', f'检测到密钥设置页 url={url[:120]}', 'WARN')
    # 优先点页面内「跳过」链（微软 passkey 页常见 id），这样连虚拟认证器都不必触发、不创建 passkey
    for sel in ('#iCancel', '#iShowSkip', '#idBtn_Back'):
        try:
            if _vis(page, sel) and D.click_sel(page, sel, timeout=2.0):
                page.wait(1)
                if log:
                    log('passkey', f'已点击跳过 {sel}', 'OK')
                return True
        except Exception:
            pass
    for text in ('暂时跳过', '现在跳过', '跳过', '以后再说', '取消', 'Skip for now',
                 'Maybe later', 'Not now', 'Cancel', 'Skip'):
        try:
            if D.click_role_button(page, text, timeout=0):
                page.wait(1)
                if log:
                    log('passkey', f'已点击 {text}', 'OK')
                return True
        except Exception:
            pass
        try:
            if D.click_if_visible(page, f'input[type="button"][value="{text}"]'):
                page.wait(1)
                if log:
                    log('passkey', f'已点击 input {text}', 'OK')
                return True
        except Exception:
            pass
    # 最后：若仍在 fido 页，直接跳回我们的 authorize（依赖 cookie）
    try:
        page.get(build_auth_url(prefer_sso=True))
        page.wait(1.2)
        if log:
            log('passkey', '密钥页无法取消，已回跳 authorize', 'WARN')
        return True
    except Exception:
        return False


def _run_cookie_recovery(page, auth_url, log, entry_timeout_ms=AUTH_ENTRY_TIMEOUT_MS):
    last_state = 'unknown'
    for method_name, action in [
        ('reload', lambda: page.refresh()),
        ('location.reload', lambda: page.run_js("location.reload()")),
        ('goto', lambda: page.get(auth_url)),
    ]:
        log('cookie_recovery', f'执行 {method_name}', 'WARN')
        try:
            action()
        except Exception as e:
            log('cookie_recovery', f'{method_name} 失败: {e}', 'WARN')
            continue
        _settle_auth_page(page, log, 'cookie_recovery')
        _disable_auth_page_autofill(page, log)
        state = _wait_for_auth_entry_state(page, timeout_ms=entry_timeout_ms)
        if state == 'account_type':
            state = _resolve_account_type(page, log)
        last_state = state
        log('cookie_recovery', f'{method_name} 后状态={state}', 'INFO')
        if state in ('consent', 'login_password', 'code'):
            return state
        if state == 'login_email':
            continue
        if state == 'account_type':
            # 已尝试点击个人帐户仍停在选择页
            continue
        if method_name == 'goto':
            return state
    return 'login_email' if last_state == 'login_email' else last_state


def _digest_post_email_states(
    page, log, state, captured_code=None, temp_mail_cfg=None,
    recovery_already_bound=False, recovery_session=None, failure_hook=None, rounds=4,
    current_email_local='',
):
    """邮箱提交后可能出现的中间页：帐户类型 / 绑定保护 / 验证辅助邮箱 / 密钥 / KMSI。"""
    for _ in range(rounds):
        if state == 'account_type':
            state = _resolve_account_type(page, log, captured_code=captured_code)
            log('account_type', f'处理后状态={state}', 'INFO')
            continue
        if state == 'protect_account':
            state = _handle_protect_account(
                page, log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                already_bound=recovery_already_bound, current_email_local=current_email_local,
            )
            log('protect_account', f'处理后状态={state}', 'INFO')
            continue
        if state == 'proof_verify':
            state = _handle_proof_verify(
                page, log, temp_mail_cfg=temp_mail_cfg,
                recovery_session=recovery_session, failure_hook=failure_hook,
            )
            log('proof_verify', f'处理后状态={state}', 'INFO')
            continue
        if state == 'kmsi':
            state = _handle_kmsi(page, log)
            log('kmsi', f'处理后状态={state}', 'INFO')
            continue
        if state == 'unknown':
            if _dismiss_passkey_setup(page, log):
                state = _wait_for_auth_state_or_code(page, captured_code, timeout_ms=12000)
                continue
            # KMSI 可能落在 unknown
            try:
                from controllers.recovery_bind import is_kmsi_page, _click_kmsi_no
                if is_kmsi_page(page):
                    _click_kmsi_no(page, log=log)
                    state = _wait_for_auth_state_or_code(page, captured_code, timeout_ms=8000)
                    continue
            except Exception:
                pass
        break
    return state


def _perform_login_after_cookie_fail(
    page, full_email, password, log, failure_hook=None, state='login_email',
    captured_code=None, temp_mail_cfg=None, recovery_already_bound=False, recovery_session=None,
):
    current_email_local = (str(full_email or '').split('@', 1)[0]).strip()
    state = _digest_post_email_states(
        page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
        recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
        failure_hook=failure_hook, rounds=4, current_email_local=current_email_local,
    )

    if state == 'login_email':
        log('login_email', '开始输入邮箱', 'WARN')
        try:
            email_stage = _submit_email(page, full_email, log)
        except Exception as exc:
            log('login_email', f'邮箱提交异常: {_compact_exc(exc)}', 'WARN')
            email_stage = _current_auth_entry_state(page)
        if email_stage in (
            'login_password', 'consent', 'code', 'account_type',
            'protect_account', 'proof_verify', 'kmsi',
        ):
            state = email_stage
        else:
            state = _wait_for_auth_state_or_code(
                page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_email'}
            )
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4, current_email_local=current_email_local,
        )
        log('login_email', f'邮箱提交后状态={state}', 'INFO')
        if _has_unknown_account(page):
            _dump_auth_page(page, log)
            log('login_email', '邮箱不存在', 'FAIL')
            return False
        if state == 'login_email':
            if failure_hook:
                failure_hook('oauth_login_timeout')
            _dump_auth_page(page, log)
            log('login_email', '邮箱页停留超时', 'FAIL')
            return False

    state = _digest_post_email_states(
        page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
        recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
        failure_hook=failure_hook, rounds=3,
    )

    if state == 'login_password':
        # 冷登录验证辅助邮箱后，有时不必再输密码；若出现密码页再填
        if _has_password_login_blocked(page):
            if failure_hook:
                failure_hook('oauth_password_blocked')
            _dump_auth_page(page, log)
            log('login_password', '密码登录不可用，跳过硬填', 'FAIL')
            return False
        log('login_password', '开始输入密码', 'WARN')
        _submit_password(page, password, log)
        if _has_password_login_blocked(page):
            if failure_hook:
                failure_hook('oauth_password_blocked')
            _dump_auth_page(page, log)
            log('login_password', '检测到密码登录不可用', 'FAIL')
            return False
        if _has_invalid_password(page):
            if failure_hook:
                failure_hook('oauth_password_wrong')
            _dump_auth_page(page, log)
            log('login_password', '检测到密码错误提示', 'FAIL')
            return False
        state = _wait_for_auth_state_or_code(
            page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_password'}
        )
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4, current_email_local=current_email_local,
        )
        log('login_password', f'密码提交后状态={state}', 'INFO')
        if state == 'code':
            return True
        if state != 'consent':
            if _has_password_login_blocked(page):
                if failure_hook:
                    failure_hook('oauth_password_blocked')
                _dump_auth_page(page, log)
                log('login_password', '密码提交后：密码登录不可用', 'FAIL')
            elif _has_invalid_password(page):
                if failure_hook:
                    failure_hook('oauth_password_wrong')
                _dump_auth_page(page, log)
                log('login_password', '检测到密码错误提示', 'FAIL')
            else:
                if failure_hook:
                    failure_hook('oauth_consent_fail')
                _dump_auth_page(page, log)
                log('login_password', f'未进入同意页面 final_state={state}', 'FAIL')
            return False

    # 冷登录常见：邮箱 → proof(codeEntry 自动验证) → kmsi 否 → consent（可能无密码页）
    if state in ('proof_verify', 'kmsi'):
        state = _digest_post_email_states(
            page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
            recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
            failure_hook=failure_hook, rounds=4, current_email_local=current_email_local,
        )
        log('proof_verify', f'proof/kmsi 处理后状态={state}', 'INFO')
        if state == 'login_password':
            # 验证后若仍要密码，再走一轮
            if not _has_password_login_blocked(page):
                log('login_password', 'proof 后出现密码页，继续填写', 'WARN')
                _submit_password(page, password, log)
                state = _wait_for_auth_state_or_code(
                    page, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS, ignore_states={'login_password'}
                )
                state = _digest_post_email_states(
                    page, log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
                    failure_hook=failure_hook, rounds=3,
                )
        if state == 'code':
            return True
        if state == 'consent':
            return True
        if state not in ('consent', 'code'):
            if failure_hook:
                failure_hook('oauth_consent_fail')
            _dump_auth_page(page, log)
            log('proof_verify', f'验证后未进入同意页 final_state={state}', 'FAIL')
            return False

    return state in ('consent', 'code')


def _exchange_code_once(code, proxy_url=None, timeout_sec=20):
    proxies = None
    if proxy_url:
        proxies = {"http": proxy_url, "https": proxy_url}
    response = requests.post(
        TOKEN_URL,
        data={
            'client_id': CLIENT_ID,
            'code': code,
            'redirect_uri': REDIRECT_URI,
            'grant_type': 'authorization_code',
            'scope': TOKEN_SCOPE,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=timeout_sec,
        proxies=proxies,
    )
    response.raise_for_status()
    return response.json()


def _exchange_code_with_retry(code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    proxy_candidates = []
    saw_network_error = False
    for item in (current_proxy,):
        if item and item not in proxy_candidates:
            proxy_candidates.append(item)
    if token_proxy_getter:
        for _ in range(2):
            try:
                picked = token_proxy_getter(exclude=proxy_candidates[-1] if proxy_candidates else current_proxy)
            except TypeError:
                picked = token_proxy_getter()
            except Exception as exc:
                log('token', f'获取新代理失败: {exc}', 'WARN')
                picked = ""
            if picked and picked not in proxy_candidates:
                proxy_candidates.append(picked)
    proxy_candidates.append("")
    total_attempts = len(proxy_candidates)
    last_error = None
    for idx in range(total_attempts):
        proxy_url = proxy_candidates[idx]
        proxy_text = proxy_url or "direct"
        try:
            log('token', f'开始换 token 第 {idx + 1}/{total_attempts} 次 proxy={proxy_text}', 'INFO')
            data = _exchange_code_once(code, proxy_url=proxy_url or None, timeout_sec=20)
            if 'refresh_token' not in data:
                last_error = RuntimeError(data.get('error_description') or data.get('error') or 'unknown')
                log('token', f"token请求失败 proxy={proxy_text}: {data.get('error', 'unknown')}", 'WARN')
                if idx < total_attempts - 1:
                    time.sleep(1.5 + idx)
                    continue
                break
            return True, data['refresh_token']
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            saw_network_error = True
            log('token', f'网络异常 proxy={proxy_text}: {exc}', 'WARN')
            if idx < total_attempts - 1:
                time.sleep(1.5 + idx)
                continue
        except Exception as exc:
            last_error = exc
            log('token', f'换 token 异常 proxy={proxy_text}: {exc}', 'WARN')
            if idx < total_attempts - 1:
                time.sleep(1.5 + idx)
                continue
    if saw_network_error and failure_hook:
        failure_hook('oauth_token_network_fail')
    if failure_hook:
        failure_hook('oauth_token_fail')
    log('token', f'最终换 token 失败: {last_error}', 'FAIL')
    return False, None


def _click_consent_and_exchange(page, captured_code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    # 点同意前先开监听，确保抓到跳转 localhost 的回调
    D.start_code_listen(page)
    accept_btn = page.ele('css:' + CONSENT_SELECTOR, timeout=60)
    if not accept_btn:
        if failure_hook:
            failure_hook('oauth_consent_fail')
        log('consent', '同意按钮未出现', 'FAIL')
        return False, None
    try:
        accept_btn.click()
    except Exception:
        accept_btn.click(by_js=True)
    log('consent', '点击接受授权', 'OK')

    code = _wait_for_code_capture(page, captured_code, timeout_ms=180000)
    if not code:
        if failure_hook:
            failure_hook('oauth_code_fail')
        log('callback', '3分钟内未捕获到code', 'FAIL')
        return False, None

    log('callback', '捕获到code', 'OK')
    return _exchange_code_with_retry(
        code,
        log=log,
        failure_hook=failure_hook,
        current_proxy=current_proxy,
        token_proxy_getter=token_proxy_getter,
    )


def _exchange_captured_code(page, captured_code, log, failure_hook=None, current_proxy="", token_proxy_getter=None):
    code = _wait_for_code_capture(page, captured_code, timeout_ms=1000, poll_ms=100)
    if not code:
        return False, None
    log('callback', '已直接捕获到code，跳过同意页', 'OK')
    return _exchange_code_with_retry(
        code,
        log=log,
        failure_hook=failure_hook,
        current_proxy=current_proxy,
        token_proxy_getter=token_proxy_getter,
    )


def get_oauth2_token(page, full_email, password, results_dir=None, prefix='', backup_proxy=None, failure_hook=None, log_hook=None, current_proxy="", token_proxy_getter=None, temp_mail_cfg=None, recovery_already_bound=False, recovery_session=None):
    # 同 context 必须 prefer_sso：不要 sso_reload，否则 cookie 会话被强制打断
    auth_url = build_auth_url(prefer_sso=True)
    current_email_local = (str(full_email or '').split('@', 1)[0]).strip()

    def _log(stage, message, level='INFO'):
        if log_hook:
            log_hook(stage, message, level)
            return
        tag = prefix if prefix else "[OAuth2:COOKIE]"
        print(f"{tag}[{level}] {time.strftime('%H:%M:%S')} | {stage} | {message}")

    def _try_flow():
        _log('start', '开始 OAuth2 (同浏览器 context 复用 cookie，无 sso_reload)')
        # 同一 BrowserContext 新开 tab，共享注册后的 login.live.com cookie
        pg = page.browser.new_tab()
        D.prepare_tab(pg)
        captured_code = [None]
        # 监听跳转到 localhost 的 OAuth 回调（含 code）
        D.start_code_listen(pg)

        try:
            t1 = time.time()
            pg.get(auth_url)
            _settle_auth_page(pg, _log, 'goto')
            _disable_auth_page_autofill(pg, _log)
            _log('goto', f"进入auth页面 (+{time.time()-t1:.0f}s)")

            # SSO 有时慢，多给一点时间再判状态
            state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=AUTH_ENTRY_TIMEOUT_MS)
            _log('entry', f'首次检测状态={state}')

            if state == 'account_type':
                state = _resolve_account_type(pg, _log, captured_code=captured_code)
                _log('entry', f'帐户类型处理后状态={state}')
            if state == 'protect_account':
                state = _handle_protect_account(
                    pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                    already_bound=recovery_already_bound, current_email_local=current_email_local,
                )
                _log('entry', f'保护帐户处理后状态={state}')
            if state == 'proof_verify':
                state = _handle_proof_verify(
                    pg, _log, temp_mail_cfg=temp_mail_cfg,
                    recovery_session=recovery_session, failure_hook=failure_hook,
                )
                _log('entry', f'proof 验证后状态={state}')
            if state == 'kmsi':
                state = _handle_kmsi(pg, _log)
                _log('entry', f'kmsi 处理后状态={state}')

            # 策略：有 cookie 的环境下，login_email 时**不要**连环 reload（会冲掉 SSO）。
            if state == 'login_email':
                _log('entry', '仍需邮箱：跳过连环 recovery，同页直接补登（保留 cookie）', 'WARN')
            elif state == 'unknown':
                _dump_auth_page(pg, _log, 'entry_unknown')
                if _is_account_type_page(pg):
                    state = _resolve_account_type(pg, _log, captured_code=captured_code)
                elif _is_protect_account_page(pg):
                    state = _handle_protect_account(
                        pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                        already_bound=recovery_already_bound,
                    )
                elif _is_proof_verify_page(pg):
                    state = _handle_proof_verify(
                        pg, _log, temp_mail_cfg=temp_mail_cfg,
                        recovery_session=recovery_session, failure_hook=failure_hook,
                    )
                elif _is_kmsi_only_page(pg):
                    state = _handle_kmsi(pg, _log)
                elif _is_login_email_page_loose(pg):
                    state = 'login_email'
                    _log('entry', 'unknown 实为邮箱登录页：同页补登当前邮箱', 'WARN')
                else:
                    _log('entry', 'unknown：单次 goto 重试 authorize', 'WARN')
                    try:
                        pg.get(auth_url)
                        _settle_auth_page(pg, _log, 'goto_retry')
                        state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=15000)
                        if state == 'unknown' and _is_login_email_page_loose(pg):
                            state = 'login_email'
                            _log('entry', 'goto 后识别为邮箱登录页：同页补登当前邮箱', 'WARN')
                    except Exception as exc:
                        _log('entry', f'goto 重试失败: {_compact_exc(exc)}', 'WARN')
                _log('entry', f'处理后状态={state}')

            if state == 'account_type':
                state = _resolve_account_type(pg, _log, captured_code=captured_code)
            if state == 'protect_account':
                state = _handle_protect_account(
                    pg, _log, temp_mail_cfg=temp_mail_cfg, failure_hook=failure_hook,
                    already_bound=recovery_already_bound, current_email_local=current_email_local,
                )
            if state == 'proof_verify':
                state = _handle_proof_verify(
                    pg, _log, temp_mail_cfg=temp_mail_cfg,
                    recovery_session=recovery_session, failure_hook=failure_hook,
                )
            if state == 'kmsi':
                state = _handle_kmsi(pg, _log)

            if state in ('login_email', 'login_password', 'account_type', 'protect_account', 'proof_verify', 'kmsi'):
                ok = _perform_login_after_cookie_fail(
                    pg,
                    full_email,
                    password,
                    _log,
                    failure_hook=failure_hook,
                    state=state,
                    captured_code=captured_code,
                    temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound,
                    recovery_session=recovery_session,
                )
                if not ok:
                    return False, None
                state = _wait_for_auth_state_or_code(pg, captured_code, timeout_ms=8000)
                state = _digest_post_email_states(
                    pg, _log, state, captured_code=captured_code, temp_mail_cfg=temp_mail_cfg,
                    recovery_already_bound=recovery_already_bound, recovery_session=recovery_session,
                    failure_hook=failure_hook, rounds=3,
                )
                _log('entry', f'登录后阶段={state}', 'INFO')

            if state == 'code':
                return _exchange_captured_code(
                    pg,
                    captured_code,
                    _log,
                    failure_hook=failure_hook,
                    current_proxy=current_proxy,
                    token_proxy_getter=token_proxy_getter,
                )
            if state == 'consent':
                return _click_consent_and_exchange(
                    pg,
                    captured_code,
                    _log,
                    failure_hook=failure_hook,
                    current_proxy=current_proxy,
                    token_proxy_getter=token_proxy_getter,
                )

            if failure_hook:
                failure_hook('oauth_consent_fail')
            _dump_auth_page(pg, _log)
            _log('entry', f'未进入同意或登录页面，最终状态={state}', 'FAIL')
            return False, None
        except Exception as e:
            _log('exception', f'异常: {_compact_exc(e)}', 'FAIL')
            return False, None
        finally:
            try:
                pg.listen.stop()
            except Exception:
                pass
            try:
                pg.close()
            except Exception:
                pass

    try:
        success, token = _try_flow()
        if success:
            return True, token
    except Exception as e:
        _log('outer', f'首次尝试异常: {_compact_exc(e)}', 'FAIL')
    return False, None
