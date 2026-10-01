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


# ── 邮箱格式校验（auth.register_user 也复用）───────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    return 0 < len(email) <= 254 and bool(_EMAIL_RE.fullmatch(email))


# ── 发送 ────────────────────────────────────────────────

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
    except Exception as e:
        raise EmailCodeError(f"邮件发送失败：{str(e)[:120]}（请检查 SMTP 配置）") from e
    return True


def send_code(email: str) -> dict:
    """生成验证码并发送。

    返回 {ok, expires_in, cooldown, sent, mail_hint}：
    - sent=False 表示走的调试模式，验证码只进了后端日志、没有投递到邮箱；
      mail_hint 说明是哪一项配置没就位。前端据此提示，避免「显示发送成功
      但收不到邮件」这种无声失败。
    """
    email = (email or "").strip().lower()
    if not is_valid_email(email):
        raise EmailCodeError("邮箱格式不正确")
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

    sent = _send_mail(email, code)
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
