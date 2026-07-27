"""微软辅助邮箱相关页面（DrissionPage 版）。

A) 注册后绑定：「让我们来保护你的帐户」
   #EmailAddress → #iNext → #iOttText → #iNext
   （与 manage-webui abuse_recovery 一致）

B) OAuth 冷登录验证已绑定邮箱（Fluent 新 UI）
   1. 验证你的电子邮件
      #proof-confirmation-email-input + button[data-testid=primaryButton]「发送验证码」
   2. 输入你的代码（6 格，无提交按钮，填完自动验证）
      #codeEntry-0 … #codeEntry-5
   3. 保持登录状态？
      button[data-testid=secondaryButton]「否」
"""
import time

from DrissionPage.common import Keys

from controllers import dp_page as D
from controllers.temp_mail import client_from_config, client_from_session

# --- 绑定页 ---
BACKUP_EMAIL_SELECTOR = "#EmailAddress"
VERIFY_CODE_SELECTOR = "#iOttText"
NEXT_SELECTOR = "#iNext"

# --- 冷登录：确认辅助邮箱并发码 ---
PROOF_EMAIL_INPUT = "#proof-confirmation-email-input"
PROOF_EMAIL_INPUT_BY_LABEL = 'label[for="proof-confirmation-email-input"]'

# --- 冷登录：6 格验证码（填完自动提交，无按钮）---
CODE_ENTRY_PREFIX = "codeEntry-"
CODE_ENTRY_COUNT = 6

# --- 保持登录 ---
KMSI_NO_BTN = 'button[data-testid="secondaryButton"]'


def is_protect_account_page(page):
    """保护帐户 / 绑定备用邮箱页（#EmailAddress）。"""
    try:
        if D.count(page, BACKUP_EMAIL_SELECTOR) > 0:
            if D.vis(page, BACKUP_EMAIL_SELECTOR):
                return True
            return True
    except Exception:
        pass
    body = D.body_text(page, limit=800)
    if "保护你的帐户" in body or "保护您的帐户" in body or "protect your account" in body.lower():
        if D.count(page, "#iShowSkip") > 0 or D.count(page, NEXT_SELECTOR) > 0:
            return True
        if "备用" in body or "电子邮件" in body:
            return True
    return False


def is_ott_code_page(page):
    """旧绑定流单框验证码 #iOttText。"""
    return D.vis(page, VERIFY_CODE_SELECTOR)


def is_code_entry_page(page):
    """Fluent 6 格验证码页：「输入你的代码」#codeEntry-0..5。"""
    if D.vis(page, f"#{CODE_ENTRY_PREFIX}0"):
        return True
    body = D.body_text(page, limit=400)
    if "输入你的代码" in body or "Enter your code" in body:
        if D.count(page, f"[id^='{CODE_ENTRY_PREFIX}']") >= 4:
            return True
    return False


def is_proof_confirm_page(page):
    """登录时「验证你的电子邮件」：确认已绑定辅助邮箱并发送验证码。"""
    if D.vis(page, PROOF_EMAIL_INPUT):
        return True
    if D.count(page, PROOF_EMAIL_INPUT_BY_LABEL) > 0:
        return True
    body = D.body_text(page, limit=900)
    if any(t in body for t in ("验证你的电子邮件", "验证您的电子邮件", "Verify your email")):
        if "发送验证码" in body or "Send code" in body or "已收到代码" in body:
            return True
        # 掩码辅助邮箱提示（不绑定具体域名）
        if "or****" in body or "or*" in body or ("@" in body and "发送" in body):
            return True
    return False


def is_kmsi_page(page):
    """保持登录状态？→ 是 / 否（secondaryButton）。"""
    body = D.body_text(page, limit=500)
    if "保持登录" in body or "Stay signed in" in body or "保持登入" in body:
        return True
    try:
        yes_btn = D.role_button(page, "是")
        no_btn = D.q(page, '[data-testid="secondaryButton"]')
        if yes_btn and no_btn and D._displayed(no_btn):
            return True
        if (
            D.role_button(page, "是")
            and D.role_button(page, "否")
            and ("登录" in body or "signed" in body.lower())
        ):
            return True
    except Exception:
        pass
    return False


def _click_i_next(page):
    for sel in (NEXT_SELECTOR, 'input#iNext', 'input[type="submit"][value="下一步"]'):
        if D.vis(page, sel):
            if D.click_sel(page, sel, timeout=5):
                return True
    if D.click_role_button(page, "下一步", timeout=1):
        return True
    return False


def _click_send_code(page):
    """点击「发送验证码」data-testid=primaryButton。"""
    if D.click_if_visible(page, '[data-testid="primaryButton"]', timeout=8):
        return True
    for text in ("发送验证码", "Send code", "Send verification code"):
        if D.click_role_button(page, text, timeout=0):
            return True
    return False


def _fill_proof_email(page, address):
    """填入完整辅助邮箱到 proof-confirmation-email-input。"""
    for sel in (PROOF_EMAIL_INPUT, 'input[type="email"]', 'input[name*="proof"]', 'input[placeholder*="电子"]'):
        el = D.q(page, sel)
        if not el or not D._displayed(el):
            continue
        try:
            el.click()
            el.input(address, clear=True)
            return True
        except Exception:
            continue
    return False


def _fill_code_entry_digits(page, code):
    """6 格 #codeEntry-0..5 逐位输入；无提交按钮，满 6 位自动验证。"""
    code = "".join(c for c in str(code) if c.isdigit())[:CODE_ENTRY_COUNT]
    if len(code) < 4:
        return False

    def _set_digit(box, ch):
        box.click()
        try:
            box.clear()
        except Exception:
            pass
        try:
            box.input(ch, clear=True)
        except Exception:
            pass
        # Fluent/React 需 input 事件才会跳格并在满位时自动提交
        try:
            box.run_js(
                """function(v){
                    const el = this;
                    const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
                    const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) { desc.set.call(el, v); }
                    else { el.value = v; }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                ch,
            )
        except Exception:
            pass

    first = page.ele(f"#{CODE_ENTRY_PREFIX}0", timeout=8)
    if first:
        try:
            for i, ch in enumerate(code):
                box = page.ele(f"#{CODE_ENTRY_PREFIX}{i}", timeout=5)
                if not box:
                    raise RuntimeError('digit box missing')
                _set_digit(box, ch)
                page.wait(0.1)
            page.wait(1.5)
            return True
        except Exception:
            pass
    # 兜底：整串输入到首格
    try:
        first = page.ele(f"#{CODE_ENTRY_PREFIX}0", timeout=2)
        if not first:
            return False
        first.click()
        try:
            first.clear()
        except Exception:
            pass
        page.actions.click(first).type(code)
        page.wait(1.5)
        return True
    except Exception:
        return False


def _click_kmsi_no(page, log=None):
    """保持登录状态？→ 点「否」data-testid=secondaryButton。"""
    def _log(msg, level="INFO"):
        if log:
            log("kmsi", msg, level)

    btn = D.q(page, '[data-testid="secondaryButton"]')
    if btn and D._displayed(btn):
        try:
            btn.click()
            _log("已点击 secondaryButton 否", "OK")
            page.wait(1.0)
            return True
        except Exception:
            pass
    for text in ("否", "No"):
        if D.click_role_button(page, text, timeout=0):
            _log(f"已点击按钮 {text}", "OK")
            page.wait(1.0)
            return True
    if D.click_if_visible(page, KMSI_NO_BTN):
        _log("已点击 KMSI_NO_BTN", "OK")
        page.wait(1.0)
        return True
    return False


def _skip_protect(page, log):
    if D.vis(page, "#iShowSkip"):
        if D.click_sel(page, "#iShowSkip", timeout=4):
            if log:
                log("recovery", "绑定失败后回退：已点 #iShowSkip 暂时跳过", "WARN")
            page.wait(0.8)
            return True
    return False


def bind_recovery_email(page, temp_mail_cfg, log=None, code_timeout=120, local_name=None):
    """在保护帐户页绑定备用邮箱并输入验证码。

    成功返回 (True, session_dict)，session 含 address/jwt 供 OAuth 冷登录复用。
    失败返回 (False, None)。
    local_name：可选，本地部分名；传当前 Outlook 邮箱名时，备用邮箱将尽量同名前缀、不同域名。
    """
    def _log(stage, msg, level="INFO"):
        if log:
            log(stage, msg, level)

    if not is_protect_account_page(page) and not is_ott_code_page(page):
        return False, None

    if is_ott_code_page(page) and not is_protect_account_page(page):
        try:
            if D.count(page, BACKUP_EMAIL_SELECTOR) == 0:
                _log("recovery", "仅代码页且无邮箱框，跳过二次绑定", "WARN")
                return True, None
        except Exception:
            pass
        _log("recovery", "已在代码页但无法新建接码会话", "FAIL")
        return False, None

    client = client_from_config(temp_mail_cfg or {})
    try:
        addr, _token = client.create_address(name=(local_name or None))
    except Exception as exc:
        _log("recovery", f"创建临时邮箱失败: {exc}", "FAIL")
        _skip_protect(page, log)
        return False, None

    session = client.session_dict()
    _log("recovery", f"临时邮箱已创建 addr={addr}（本任务独立会话，provider={session.get('provider')}）", "OK")
    after_ts = time.time()

    try:
        email_box = page.ele('css:' + BACKUP_EMAIL_SELECTOR, timeout=10)
        if not email_box:
            raise RuntimeError("备用邮箱框未出现")
        email_box.click()
        email_box.input(addr, clear=True)
        page.wait(0.3)
        if not _click_i_next(page):
            raise RuntimeError("无法点击下一步提交备用邮箱")
        _log("recovery", f"已提交备用邮箱 {addr}", "INFO")
    except Exception as exp:
        _log("recovery", f"填写备用邮箱失败: {exp}", "FAIL")
        _skip_protect(page, log)
        return False, None

    if not page.ele('css:' + VERIFY_CODE_SELECTOR, timeout=30):
        if is_protect_account_page(page):
            _log("recovery", "提交后仍在保护帐户页", "WARN")
            _skip_protect(page, log)
            return False, None
        if not is_ott_code_page(page):
            _log("recovery", "未出现验证码输入框，视为可能已完成", "WARN")
            return True, session

    page.wait(2.0)
    code = client.wait_for_code(
        timeout_sec=int((temp_mail_cfg or {}).get("code_timeout", code_timeout)),
        poll_sec=float((temp_mail_cfg or {}).get("poll_interval", 3)),
        after_ts=after_ts - 5,
        log=log,
    )
    if not code:
        _log("recovery", "未收到微软验证码，尝试暂时跳过", "FAIL")
        try:
            page.back()
            page.wait(1.0)
            _skip_protect(page, log)
        except Exception:
            pass
        return False, None

    try:
        ott = page.ele('css:' + VERIFY_CODE_SELECTOR, timeout=5)
        if not ott:
            raise RuntimeError("验证码框消失")
        ott.click()
        ott.input(code, clear=True)
        page.wait(0.3)
        if not _click_i_next(page):
            raise RuntimeError("无法点击下一步提交验证码")
        _log("recovery", f"已提交验证码 code={code}", "OK")
        page.wait(1.5)
        return True, session
    except Exception as exc:
        _log("recovery", f"提交验证码失败: {exc}", "FAIL")
        return False, None


def _client_from_session(session, temp_mail_cfg):
    """用绑定阶段保存的会话重建 client（按 provider 分发），才能收同一邮箱的验证码。"""
    return client_from_session(session, temp_mail_cfg or {})


def verify_bound_email_on_login(page, bound_session, temp_mail_cfg, log=None, code_timeout=180):
    """OAuth 冷登录验证已绑定辅助邮箱全流程。

    bound_session: {address, jwt, ...} 注册绑定阶段保存。
    步骤：
      1) 填 #proof-confirmation-email-input + 点「发送验证码」
      2) 轮询临时邮箱取码 → 填 #codeEntry-0..5（无提交按钮，自动验证）
      3) 若出现「保持登录」→ 点 secondaryButton「否」
    """
    def _log(stage, msg, level="INFO"):
        if log:
            log(stage, msg, level)

    # 仅「保持登录」页：点否即可
    if is_kmsi_page(page) and not is_proof_confirm_page(page) and not is_code_entry_page(page):
        ok = _click_kmsi_no(page, log=log)
        if ok:
            _log("proof_verify", "仅 KMSI 页，已点「否」", "OK")
        return ok

    if not bound_session or not bound_session.get("address"):
        _log("proof_verify", "无已绑定辅助邮箱会话，无法验证", "FAIL")
        return False

    bound_address = bound_session["address"]
    client = _client_from_session(bound_session, temp_mail_cfg)
    if client is None:
        _log("proof_verify", "缺少绑定阶段 jwt，无法接码", "FAIL")
        return False

    # --- 发码页 ---
    if is_proof_confirm_page(page):
        if not _fill_proof_email(page, bound_address):
            _log("proof_verify", f"无法填写辅助邮箱框 addr={bound_address}", "FAIL")
            return False
        _log("proof_verify", f"已填写辅助邮箱 {bound_address}", "INFO")
        page.wait(0.3)
        after_ts = time.time()
        if not _click_send_code(page):
            _log("proof_verify", "无法点击「发送验证码」", "FAIL")
            return False
        _log("proof_verify", "已点击发送验证码", "OK")
    elif is_code_entry_page(page) or is_ott_code_page(page):
        after_ts = time.time() - 30
        _log("proof_verify", "已在代码页，直接接码", "INFO")
    else:
        return False

    # 等 6 格或旧单框
    code_ready = False
    for _ in range(40):
        if is_code_entry_page(page) or is_ott_code_page(page):
            code_ready = True
            break
        # 「已收到代码」入口
        try:
            for text in ("已收到代码", "I have a code", "I already have a code"):
                if D.click_if_visible(page, f'text:{text}', timeout=0):
                    page.wait(0.8)
                    break
        except Exception:
            pass
        page.wait(0.5)

    if not code_ready:
        _log("proof_verify", "未出现验证码输入页", "FAIL")
        return False

    page.wait(1.5)
    code = client.wait_for_code(
        timeout_sec=int((temp_mail_cfg or {}).get("code_timeout", code_timeout)),
        poll_sec=float((temp_mail_cfg or {}).get("poll_interval", 3)),
        after_ts=after_ts - 5,
        log=log,
    )
    if not code:
        _log("proof_verify", "未收到验证码", "FAIL")
        return False

    # 填码
    if is_code_entry_page(page):
        if not _fill_code_entry_digits(page, code):
            _log("proof_verify", f"6 格填码失败 code={code}", "FAIL")
            return False
        _log("proof_verify", f"已填入 6 格验证码 code={code}（自动提交）", "OK")
    else:
        try:
            ott = page.ele('css:' + VERIFY_CODE_SELECTOR, timeout=5)
            if not ott:
                raise RuntimeError("单框验证码框未出现")
            ott.click()
            ott.input(code, clear=True)
            if not _click_i_next(page):
                page.actions.type(Keys.ENTER)
            _log("proof_verify", f"已提交单框验证码 code={code}", "OK")
        except Exception as exc:
            _log("proof_verify", f"单框填码失败: {exc}", "FAIL")
            return False

    # 等跳转 / KMSI
    page.wait(2.0)
    for _ in range(15):
        if is_kmsi_page(page):
            if _click_kmsi_no(page, log=log):
                _log("proof_verify", "已点保持登录「否」", "OK")
            break
        # 已到 consent / 其它页
        try:
            if D.count(page, '[data-testid="appConsentPrimaryButton"]') > 0:
                break
            if "localhost" in (page.url or "") and "code=" in (page.url or ""):
                break
        except Exception:
            pass
        page.wait(0.4)

    # 再扫一次 KMSI（有时慢）
    if is_kmsi_page(page):
        _click_kmsi_no(page, log=log)

    return True
