"""邮箱验证码（注册用）：发送 / 存储 / 校验

- 发信走标准库 smtplib，零新依赖。
- 未配置 SMTP（MAIL_SMTP_HOST 为空）或 MAIL_DEBUG=1 时进入调试模式：
  验证码打印到后端日志/控制台，不真正发信，便于本地先把流程跑通；
  配置好 SMTP 后自动切换为真实发送（MAIL_DEBUG=0）。
- 验证码内存存储：默认 5 分钟有效、最多尝试 5 次、一次性消费、60 秒重发冷却。
- 生产多进程部署时建议把 _CODES 换成 Redis（项目已依赖 redis）。
"""

from __future__ import annotations

import os
import re
import secrets
import smtplib
import threading
import time
from email.header import Header as EmailHeader
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

# 本模块用 os.getenv 直接读配置，而 load_dotenv 原先只在 config.py 里被调用。
# 一旦本模块先于 config 被导入（脚本、测试），.env 里的 MAIL_* 就读不到，
# 于是静默退回调试模式——验证码看着「发送成功」却永远收不到。
# 这里自己补一次加载，指向同目录的 .env；load_dotenv 默认不覆盖已存在的
# 环境变量，与 config.py 的调用重复也不会互相干扰。
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).with_name(".env"))
except Exception:
    pass

# ── 配置读取（每次调用读 env，避免导入顺序问题）────────────

def _env(key: str, default: str = "") -> str:
    return os.getenv(key, "").strip() if os.getenv(key, "").strip() else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except Exception:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key, "")
    if v.strip() == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# ── 存储（内存 + 锁）────────────────────────────────────

_CODES: dict[str, dict] = {}   # email -> {code, expires_at, attempts, sent_at}
_SENT_AT: dict[str, int] = {}  # email -> 最近一次发送时间戳（与验证码生命周期解耦，消费后仍限流）
_lock = threading.RLock()


class EmailCodeError(ValueError):
    """邮箱/发信类错误（业务错误，返回 400）"""


class EmailCooldownError(EmailCodeError):
    """发送过于频繁（返回 429）"""


class EmailRecipientError(EmailCodeError):
    """收件地址不存在/被拒（SMTP 550）：属于用户填错邮箱，不是服务端配置问题（返回 400）"""


# 出现 550 即代表该地址在邮件服务器上不存在——用普通用户能看懂的话说明
_RECIPIENT_NOT_FOUND = (
    "该邮箱不存在，请检查邮箱地址是否填写正确"
    "（常见原因：字母打错、多写或少写字符、域名后缀写错）。"
)


# ── 邮箱格式校验（auth.register_user 也复用）───────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    return 0 < len(email) <= 254 and bool(_EMAIL_RE.fullmatch(email))


# 面向普通用户的中文提示（不暴露正则、状态码、SMTP 术语）
_EMAIL_HINT = "正确格式示例：name@example.com"


def email_format_error(email: str) -> str:
    """邮箱格式体检：返回可直接展示给普通用户的中文原因；通过则返回空串。

    逐条给出具体原因（而不是笼统的「格式不正确」），避免用户反复试错：
    中文输入法下极易打出全角 ＠ / ． / 空格，肉眼几乎看不出，是最常见的成因。
    注意本函数的严格度 ≥ is_valid_email（末尾有 _EMAIL_RE 兜底），
    因此不会出现「发码放行、注册却报错」的不一致。
    """
    raw = (email or "").strip()
    if not raw:
        return "请填写邮箱地址"
    if any(ord(ch) > 0x7F for ch in raw):
        return f"邮箱地址里有中文或全角字符，请切换到英文输入法重新输入（{_EMAIL_HINT}）"
    if " " in raw or "\t" in raw:
        return "邮箱地址中间不能有空格"
    if "@" not in raw:
        return f"邮箱地址缺少 @ 符号（{_EMAIL_HINT}）"
    if raw.count("@") > 1:
        return "邮箱地址里出现了多个 @ 符号"
    local, _, domain = raw.partition("@")
    if not local:
        return f"@ 前面缺少邮箱名称（{_EMAIL_HINT}）"
    if not domain:
        return f"@ 后面缺少邮箱域名（{_EMAIL_HINT}）"
    if "." not in domain:
        return f"邮箱域名缺少后缀，如 @qq.com 里的 .com（{_EMAIL_HINT}）"
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        return "邮箱域名的点号位置不对，请检查域名部分"
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return "邮箱名称不能以点号开头或结尾，也不能出现连续两个点号"
    if any(part.startswith("-") or part.endswith("-") for part in domain.split(".")):
        return "邮箱域名的连字符位置不对，请检查域名部分"
    if len(raw) > 254:
        return "邮箱地址过长，请检查是否多输了内容"
    if not _EMAIL_RE.fullmatch(raw):
        return f"邮箱格式不正确（{_EMAIL_HINT}）"
    return ""


# ── 发送 ────────────────────────────────────────────────

def _as_text(value) -> str:
    """SMTP 服务器响应常以 bytes 形式挂在异常上，直接 str() 会打印成 b'...' 难以阅读。"""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def _smtp_detail(exc: Exception) -> str:
    """把 smtplib 异常转成可读的「状态码 + 服务器原文」。"""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "；".join(f"{code} {_as_text(msg)}" for code, msg in exc.recipients.values())
    code = getattr(exc, "smtp_code", None)
    if code is not None:
        return f"{code} {_as_text(getattr(exc, 'smtp_error', ''))}"
    return _as_text(exc)


def _log_smtp_failure(kind: str, to_email: str, exc: Exception) -> None:
    """把服务器返回的原始状态码/英文原因写进后端日志。

    排障确实需要这些细节，但**不能**塞进 HTTP 响应——此前 550 的英文原文被直接
    抛给前端，用户看到 "(550, b'The recipient may contain a non-existent account...')"
    完全不知道该改什么。响应只给人话，细节留给管理员看日志。
    """
    try:
        import logging
        logging.getLogger("email_codes").warning(
            "邮件发送失败 [%s] 收件人=%s 原因=%s", kind, to_email, _smtp_detail(exc)
        )
    except Exception:
        pass


def mail_status() -> tuple[bool, str]:
    """邮件是否已配置齐全、可真实发送。返回 (可发送, 不可发送的原因)。

    单独抽出来判定，是因为「只填了 MAIL_SMTP_HOST、漏填授权码」这种情况
    若直接去连 SMTP，会抛认证失败，用户看到的是「邮件发送失败」，排查方向
    会被带偏成「邮箱服务商有问题」。提前判定后，提示直接指向缺哪一项。
    """
    host = _env("MAIL_SMTP_HOST")
    user = _env("MAIL_SMTP_USER")
    pwd = _env("MAIL_SMTP_PASSWORD")
    if not host:
        return False, "MAIL_SMTP_HOST 未配置"
    if not user or not pwd:
        return False, ("MAIL_SMTP_USER / MAIL_SMTP_PASSWORD 未填写完整"
                       "（QQ 邮箱此处填 16 位授权码，不是登录密码）")
    if _env_bool("MAIL_DEBUG", default=False):
        return False, "MAIL_DEBUG=1，已强制调试模式"
    return True, ""


def _send_mail(to_email: str, code: str) -> bool:
    """发送验证码邮件。返回是否真实投递（False = 调试模式，验证码只打印到后端日志）。"""
    ttl = _env_int("MAIL_CODE_TTL", 600)
    minutes = max(1, ttl // 60)

    ready, why = mail_status()
    if not ready:
        msg = (f"[邮箱验证码·调试模式，未真实发送] 原因：{why}。"
               f"收件人 {to_email} 的注册验证码为：{code}，{minutes} 分钟内有效。"
               f"在 backend/.env 补全 MAIL_SMTP_HOST / MAIL_SMTP_USER / MAIL_SMTP_PASSWORD"
               f"（QQ 邮箱填授权码）并保持 MAIL_DEBUG=0，重启后端后即切换为真实发送。")
        print(msg, flush=True)
        try:
            import logging
            logging.getLogger(__name__).warning(msg)
        except Exception:
            pass
        return False

    host = _env("MAIL_SMTP_HOST")
    user = _env("MAIL_SMTP_USER")
    pwd = _env("MAIL_SMTP_PASSWORD")
    sender = _env("MAIL_FROM") or user
    from_name = _env("MAIL_FROM_NAME") or "Vequo 维阔"
    port = _env_int("MAIL_SMTP_PORT", 465)

    body = (f"您好：\n\n您正在注册 Vequo 维阔 账号，本次验证码为：\n\n    {code}\n\n"
            f"验证码 {minutes} 分钟内有效，请勿泄露给他人。\n如非本人操作，请忽略本邮件。\n\n"
            f"—— {from_name}")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = EmailHeader(f"【{from_name}】注册验证码", "utf-8")
    msg["From"] = formataddr((str(EmailHeader(from_name, "utf-8")), sender))
    msg["To"] = to_email

    try:
        if port == 465:
            # QQ/163 等常用 SSL 直连端口
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                s.login(user, pwd)
                s.sendmail(sender, [to_email], msg.as_string())
        else:
            # 587 等 STARTTLS 端口
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                s.login(user, pwd)
                s.sendmail(sender, [to_email], msg.as_string())
    # 顺序敏感：SMTPException 自 Python 3.4 起继承 OSError，
    # 所有 smtplib 分支必须排在 OSError 之前，否则会被 OSError 兜底吞掉。
    # 文案原则：响应只给普通用户看得懂的中文；服务器原文/状态码只进日志（_log_smtp_failure）。
    except smtplib.SMTPRecipientsRefused as e:
        _log_smtp_failure("recipient_refused", to_email, e)
        raise EmailRecipientError(_RECIPIENT_NOT_FOUND) from e
    except smtplib.SMTPAuthenticationError as e:
        _log_smtp_failure("auth_failed", to_email, e)
        raise EmailCodeError("邮件服务暂时不可用，请联系管理员检查发信邮箱配置。") from e
    except smtplib.SMTPSenderRefused as e:
        _log_smtp_failure("sender_refused", to_email, e)
        raise EmailCodeError("邮件服务暂时不可用，请联系管理员检查发信邮箱配置。") from e
    except smtplib.SMTPConnectError as e:
        _log_smtp_failure("connect_error", to_email, e)
        raise EmailCodeError("暂时无法连接邮件服务，请稍后重试。") from e
    except smtplib.SMTPResponseException as e:
        _log_smtp_failure("response_%s" % getattr(e, "smtp_code", "?"), to_email, e)
        # 550 也可能出现在 DATA 阶段（QQ 把「收件人不存在」的判定推迟到这里）
        if getattr(e, "smtp_code", None) == 550:
            raise EmailRecipientError(_RECIPIENT_NOT_FOUND) from e
        raise EmailCodeError("邮件发送失败，请稍后重试；若多次失败请联系管理员。") from e
    except smtplib.SMTPServerDisconnected as e:
        _log_smtp_failure("server_disconnected", to_email, e)
        raise EmailCodeError("邮件服务连接中断，请稍后重试。") from e
    except OSError as e:
        # 含 socket 超时 / DNS 解析失败 / 网络不可达
        _log_smtp_failure("network", to_email, e)
        raise EmailCodeError("网络异常，暂时无法发送验证码，请稍后重试。") from e
    return True


def send_code(email: str) -> dict:
    """生成验证码并发送。

    返回 {ok, expires_in, cooldown, sent, mail_hint}：
    - sent=False 表示走的调试模式，验证码只进了后端日志、没有投递到邮箱；
      mail_hint 说明是哪一项配置没就位。前端据此提示，避免「显示发送成功
      但收不到邮件」这种无声失败。
    """
    email = (email or "").strip().lower()
    reason = email_format_error(email)
    if reason:
        raise EmailCodeError(reason)
    from auth import find_user, find_user_by_email, audit
    if find_user(email) or find_user_by_email(email):
        raise EmailCodeError("该邮箱已注册，请直接登录")

    now = int(time.time())
    cooldown = _env_int("MAIL_CODE_RESEND_SECONDS", 60)
    ttl = _env_int("MAIL_CODE_TTL", 600)
    with _lock:
        # 冷却以「最近一次发送时间」为准（与验证码是否仍存在无关）：
        # 验证码被消费/过期后同样受限流约束，避免反复取码刷接口
        remain = _SENT_AT.get(email, 0) + cooldown - now
        if remain > 0:
            raise EmailCooldownError(f"发送过于频繁，请 {remain} 秒后再试")
        code = f"{secrets.randbelow(10**6):06d}"
        _CODES[email] = {"code": code, "expires_at": now + ttl, "attempts": 0, "sent_at": now}
        _SENT_AT[email] = now

    try:
        sent = _send_mail(email, code)
    except Exception:
        # 发信失败必须回滚占位：否则用户改正邮箱后要白等一整个冷却期（默认 60s），
        # 且 _CODES 里会留一份永远送不到的死码。
        with _lock:
            if _SENT_AT.get(email) == now:
                _SENT_AT.pop(email, None)
            rec = _CODES.get(email)
            if rec and rec.get("code") == code:
                _CODES.pop(email, None)
        raise
    audit("email_code_sent", email=email, ttl=ttl, delivered=sent)
    _, why = mail_status()
    return {
        "ok": True,
        "expires_in": ttl,
        "cooldown": cooldown,
        "sent": sent,
        "mail_hint": "" if sent else why,
    }


def verify_code(email: str, code: str) -> bool:
    """校验验证码：存在 / 未过期 / 尝试 ≤5 次；成功即一次性消费（删除）。"""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    now = int(time.time())
    with _lock:
        rec = _CODES.get(email)
        if not rec:
            return False
        if now > rec["expires_at"]:
            _CODES.pop(email, None)
            return False
        if rec["attempts"] >= 5:
            _CODES.pop(email, None)
            return False
        if not secrets.compare_digest(rec["code"], code):
            rec["attempts"] += 1
            return False
        _CODES.pop(email, None)  # 一次性消费
        return True