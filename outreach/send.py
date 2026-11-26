#!/usr/bin/env python3
"""유어팀 제휴 제안 메일 — 매일 자동 발송 (토큰 없이 코드로만 동작).

GitHub Actions 가 매일 아침 실행합니다.
  1) 다음 메일함에서 '수신거부' 회신과 반송 메일을 찾아 시트의 "수신거부" 탭에 등록
  2) 시트(Apps Script)에서 오늘 보낼 대상 목록을 받아옴
  3) 다음(Daum) 메일 서버로 1명에게 1통씩 발송 (받는 사람에게는 자기 주소만 보임)
  4) 10통마다 시트에 결과 기록, 끝나면 보고 메일 1통

필요한 환경변수 (GitHub → Settings → Secrets and variables → Actions)
  SHEET_API_URL    Apps Script 웹 앱 주소
  SHEET_API_TOKEN  Apps Script setup() 이 만들어 준 토큰
  SMTP_USER        다음 메일 접속 아이디 (예: jeongji1228)
  SMTP_PASS        다음 메일 앱 비밀번호
  REPORT_TO        결과 보고를 받을 주소
선택
  MODE             send(기본) | dry-run(대상만 확인) | test(REPORT_TO 로 1통만)
  DAILY_LIMIT      숫자를 넣으면 아래 일정 대신 이 값 사용

공개 저장소이므로 로그에는 받는 사람 주소·업체명을 남기지 않습니다(개수만).
"""

from __future__ import annotations

import datetime as dt
import email
import email.header
import email.policy
import imaplib
import json
import os
import random
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid, parseaddr
from pathlib import Path

HERE = Path(__file__).resolve().parent
KST = dt.timezone(dt.timedelta(hours=9))

# ───────── 설정 ─────────
FROM_ADDR = "jiwon@ur-team.com"
FROM_NAME = "정지원(유어팀)"
SUBJECT = "[제휴 제안] 네이버 광고 예산 페이백 제휴 제안의 건"
SMTP_HOST, SMTP_PORT = "smtp.daum.net", 465
IMAP_HOST, IMAP_PORT = "imap.daum.net", 993

# 날짜별 하루 발송 수 (한국 날짜 기준, 위에서부터 처음 맞는 줄 사용)
SCHEDULE = [
    ("2026-12-03", 150),   # 이 날짜까지(포함) 150통
    ("9999-12-31", 500),   # 그 뒤로 500통
]

DELAY_RANGE = (4.0, 8.0)        # 메일 사이 쉬는 시간(초) — 한꺼번에 몰리지 않게
RECORD_EVERY = 10               # 몇 통마다 시트에 기록할지
RECONNECT_EVERY = 50            # 몇 통마다 메일 서버에 다시 접속할지
MAX_CONSECUTIVE_ERRORS = 3      # 연속 오류가 이만큼 나면 그날은 중단
UNSUB_LOOKBACK_DAYS = 14        # 수신거부·반송 확인할 기간
UNSUB_WORDS = ("수신거부", "수신 거부", "거부합니다", "보내지 마", "unsubscribe")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def env(name: str, required: bool = True, default: str = "") -> str:
    v = os.environ.get(name, "").strip()
    if required and not v:
        sys.exit(f"환경변수 {name} 가 비어 있습니다. GitHub Secrets 설정을 확인하세요.")
    return v or default


def today_limit(today: dt.date) -> int:
    override = os.environ.get("DAILY_LIMIT", "").strip()
    if override.isdigit():
        return int(override)
    for until, n in SCHEDULE:
        if today <= dt.date.fromisoformat(until):
            return n
    return SCHEDULE[-1][1]


# ───────── 시트(Apps Script) 연결 ─────────
class Sheet:
    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    def _call(self, action: str, params: dict | None = None, body: dict | None = None) -> dict:
        q = {"action": action, "token": self.token, **(params or {})}
        full = self.url + ("&" if "?" in self.url else "?") + urllib.parse.urlencode(q)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(full, data=data, headers={"Content-Type": "application/json"})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    res = json.loads(r.read().decode())
                if not res.get("ok"):
                    raise RuntimeError(res.get("error", "unknown error"))
                return res
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    raise
                log(f"시트 연결 재시도({attempt + 1}): {e}")
                time.sleep(5 * (attempt + 1))
        raise AssertionError

    def targets(self, limit: int) -> dict:
        return self._call("targets", {"limit": limit})

    def record(self, rows: list[dict]) -> None:
        if rows:
            self._call("record", body={"rows": rows})

    def suppress(self, items: list[dict]) -> int:
        if not items:
            return 0
        return self._call("suppress", body={"items": items}).get("added", 0)


# ───────── 메일 만들기 ─────────
def html_to_text(html: str) -> str:
    t = re.sub(r"\s*\n\s*", "", html)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</tr>|</ol>", "\n", t)
    t = re.sub(r"(?i)<li[^>]*>", "· ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def build_message(to_addr: str, html: str, text: str, banner: bytes, subject: str = SUBJECT) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = Address(FROM_NAME, addr_spec=FROM_ADDR)
    msg["To"] = to_addr                      # 받는 사람은 항상 1명
    msg["Subject"] = subject
    msg["Date"] = format_datetime(dt.datetime.now(KST))
    msg["Message-ID"] = make_msgid(domain=FROM_ADDR.split("@")[1])
    msg["Reply-To"] = FROM_ADDR
    unsub = urllib.parse.quote("수신거부")
    msg["List-Unsubscribe"] = f"<mailto:{FROM_ADDR}?subject={unsub}>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    msg.get_payload()[1].add_related(banner, "image", "png", cid="<banner>", filename="urteam.png")
    return msg


# ───────── 다음 메일 서버 ─────────
class Mailer:
    def __init__(self, user: str, password: str):
        self.user, self.password = user, password
        self.smtp: smtplib.SMTP_SSL | None = None
        self.count = 0

    def connect(self) -> None:
        self.close()
        self.smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=60)
        self.smtp.login(self.user, self.password)
        self.count = 0

    def close(self) -> None:
        if self.smtp:
            try:
                self.smtp.quit()
            except Exception:  # noqa: BLE001
                pass
        self.smtp = None

    def send(self, msg: EmailMessage, to_addr: str) -> None:
        if self.smtp is None or self.count >= RECONNECT_EVERY:
            self.connect()
        try:
            self.smtp.send_message(msg, from_addr=FROM_ADDR, to_addrs=[to_addr])
        except smtplib.SMTPServerDisconnected:
            self.connect()
            self.smtp.send_message(msg, from_addr=FROM_ADDR, to_addrs=[to_addr])
        self.count += 1


def is_fatal(err: Exception) -> bool:
    """하루 한도·인증 문제처럼 계속 보내면 안 되는 오류인지."""
    s = str(err).lower()
    if isinstance(err, smtplib.SMTPAuthenticationError):
        return True
    return any(k in s for k in ("quota", "limit", "exceed", "too many", "rate", "spam", "blocked", "421", "454", "550 5.7"))


# ───────── 수신거부·반송 찾기 (다음 메일함) ─────────
def decode_header(v: str | None) -> str:
    if not v:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(v)))
    except Exception:  # noqa: BLE001
        return v


def body_text(m: email.message.Message, limit: int = 20000) -> str:
    parts = []
    for p in m.walk():
        if p.get_content_maintype() == "multipart":
            continue
        ctype = p.get_content_type()
        if ctype == "message/delivery-status":
            parts.append(str(p))
        elif ctype.startswith("text/"):
            try:
                payload = p.get_payload(decode=True) or b""
                parts.append(payload.decode(p.get_content_charset() or "utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass
    return "\n".join(parts)[:limit]


def scan_mailbox(user: str, password: str) -> list[dict]:
    items: list[dict] = []
    since = (dt.datetime.now(KST) - dt.timedelta(days=UNSUB_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, password)
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, "SINCE", since)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        for mid in ids[-1500:]:
            typ, hd = imap.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if typ != "OK" or not hd or not isinstance(hd[0], tuple):
                continue
            h = email.message_from_bytes(hd[0][1])
            frm = parseaddr(decode_header(h.get("From")))[1].lower()
            subj = decode_header(h.get("Subject"))
            bounce = frm.startswith(("mailer-daemon", "postmaster")) or "delivery status" in subj.lower() \
                or "undeliver" in subj.lower() or "반송" in subj or "전송 실패" in subj
            reply = "제휴 제안" in subj or any(w in subj for w in UNSUB_WORDS)
            if not (bounce or reply):
                continue
            typ, full = imap.fetch(mid, "(BODY.PEEK[])")
            if typ != "OK" or not full or not isinstance(full[0], tuple):
                continue
            m = email.message_from_bytes(full[0][1])
            text = body_text(m)
            if bounce:
                found = re.findall(r"(?im)^(?:final|original)-recipient:\s*rfc822;\s*<?([^\s>]+@[^\s>]+)>?", text)
                if not found:
                    found = re.findall(r"<([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>", text)[:1]
                for e in found:
                    if e.lower() != FROM_ADDR:
                        items.append({"email": e.lower(), "reason": "반송"})
            elif frm and frm != FROM_ADDR and (any(w in subj for w in UNSUB_WORDS) or
                                               any(w in text[:3000] for w in UNSUB_WORDS)):
                items.append({"email": frm, "reason": "수신거부 회신"})
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
    return items


# ───────── 실행 ─────────
def main() -> None:
    mode = env("MODE", required=False, default="send").lower()
    smtp_user, smtp_pass = env("SMTP_USER"), env("SMTP_PASS")
    report_to = env("REPORT_TO")
    html = (HERE / "template.html").read_text(encoding="utf-8")
    text = html_to_text(html)
    banner = (HERE / "banner.png").read_bytes()
    today = dt.datetime.now(KST).date()

    if mode == "test":
        mailer = Mailer(smtp_user, smtp_pass)
        mailer.send(build_message(report_to, html, text, banner), report_to)
        mailer.close()
        log("테스트 메일 1통 발송 완료 (REPORT_TO 로). 시트에는 기록하지 않았습니다.")
        return

    sheet = Sheet(env("SHEET_API_URL"), env("SHEET_API_TOKEN"))

    # 1) 수신거부·반송 반영
    try:
        found = scan_mailbox(smtp_user, smtp_pass)
        added = sheet.suppress(found) if mode == "send" else 0
        log(f"수신거부·반송 확인: 찾은 {len(found)}건, 새로 등록 {added}건")
    except Exception as e:  # noqa: BLE001
        log(f"수신거부 확인은 건너뜀(발송은 계속): {type(e).__name__}")

    # 2) 대상 받아오기
    limit = today_limit(today)
    res = sheet.targets(limit)
    targets, remaining = res["targets"], res["remaining"]
    log(f"오늘 목표 {limit}통 · 받아온 대상 {len(targets)}곳 · 남은 전체 대상 {remaining}곳")
    if mode == "dry-run":
        doms: dict[str, int] = {}
        for t in targets:
            d = t["email"].split("@")[1].lower()
            doms[d] = doms.get(d, 0) + 1
        log("대상 메일 종류(상위): " + ", ".join(f"{d} {n}" for d, n in sorted(doms.items(), key=lambda x: -x[1])[:8]))
        log("dry-run 이라 발송하지 않았습니다.")
        return

    # 3) 1통씩 발송
    mailer = Mailer(smtp_user, smtp_pass)
    started = time.time()
    pending: list[dict] = []
    ok = fail = 0
    consecutive = 0
    stop_reason = ""
    for i, t in enumerate(targets, 1):
        ts = dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M")
        try:
            mailer.send(build_message(t["email"], html, text, banner), t["email"])
            pending.append({**t, "ts": ts, "result": "보냄", "ok": True})
            ok += 1
            consecutive = 0
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError, smtplib.SMTPSenderRefused,
                smtplib.SMTPException, OSError) as e:
            consecutive += 1
            if is_fatal(e):
                # 한도·인증 문제: 이 업체는 기록하지 않음(다음 날 다시 보냄)
                log(f"{i}번째에서 서버 오류: {type(e).__name__} {str(e)[:120]}")
                if consecutive >= MAX_CONSECUTIVE_ERRORS or isinstance(e, smtplib.SMTPAuthenticationError):
                    stop_reason = f"메일 서버 한도/인증 오류로 중단: {str(e)[:200]}"
                    break
                mailer.close()
                time.sleep(30)
                continue
            pending.append({**t, "ts": ts, "result": f"실패: {str(e)[:150]}", "ok": False})
            fail += 1
            if consecutive >= MAX_CONSECUTIVE_ERRORS * 3:
                stop_reason = f"연속 실패가 많아 중단: {str(e)[:200]}"
                break
        if len(pending) >= RECORD_EVERY:
            sheet.record(pending)
            pending = []
        if i % 50 == 0:
            log(f"진행 {i}/{len(targets)} (성공 {ok}, 실패 {fail})")
        if i < len(targets):
            time.sleep(random.uniform(*DELAY_RANGE))
    sheet.record(pending)
    mailer.close()

    minutes = (time.time() - started) / 60
    left = max(remaining - ok - fail, 0)
    summary = (
        f"날짜: {today}\n오늘 목표: {limit}통\n성공: {ok}통\n실패: {fail}통\n"
        f"걸린 시간: 약 {minutes:.0f}분\n남은 대상: 약 {left}곳\n"
        + (f"중단 사유: {stop_reason}\n" if stop_reason else "")
        + "\n자세한 내역은 시트의 '자동발송_로그' 탭을 확인하세요."
    )
    log(summary.replace("\n", " | "))

    # 4) 보고 메일
    try:
        rep = EmailMessage(policy=email.policy.SMTP)
        rep["From"] = Address(FROM_NAME, addr_spec=FROM_ADDR)
        rep["To"] = report_to
        rep["Subject"] = f"[자동발송 보고] {today} 제휴 제안 메일 결과"
        rep["Date"] = format_datetime(dt.datetime.now(KST))
        rep["Message-ID"] = make_msgid(domain=FROM_ADDR.split("@")[1])
        rep.set_content(summary)
        m2 = Mailer(smtp_user, smtp_pass)
        m2.send(rep, report_to)
        m2.close()
    except Exception as e:  # noqa: BLE001
        log(f"보고 메일 실패: {type(e).__name__}")

    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write("## 제휴 제안 메일 발송 결과\n\n```\n" + summary + "\n```\n")
    if stop_reason and ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
