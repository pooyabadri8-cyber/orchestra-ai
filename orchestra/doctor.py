"""Health check: everything this computer needs, checked in one go, with what to do about each problem (in Persian).

    python -m orchestra.cli check     (check.bat)        or  /check in the control bot

Read-only, except one tiny model call (to know the key and the model really answer). Nothing is published, no data is
stored, no secret is printed. Every outside contact (bot API, model, sources, browser) can be replaced, so the checks
themselves are tested without a network.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

OK, WARN, FAIL = "ok", "warn", "fail"
ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


class Doctor:
    def __init__(self, cfg, org: str, *, sessions, rtm=None, runtime_error: str = "", destinations=(),
                 bot_factory: Callable | None = None, browser_probe: Callable | None = None,
                 project: Path | None = None, now: datetime | None = None):
        self.cfg, self.org, self.sessions, self.rtm, self.runtime_error = cfg, org, sessions, rtm, runtime_error
        self.destinations = list(destinations)
        self.bot_factory = bot_factory or _bot
        self.browser_probe = browser_probe or _browser
        self.project = project or Path.cwd()
        from .db import utcnow
        self.now = now or utcnow()
        self.out: list[Check] = []

    def add(self, area: str, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.out.append(Check(area, name, status, detail, fix))

    async def run(self) -> list[Check]:
        self.files()
        await self.model()
        self.bots()
        self.channels()
        await self.sources()
        await self.news()
        await self.slides()
        self.backup()
        return self.out

    # ------------------------------------------------------------------ settings files

    def files(self) -> None:
        root = self.cfg.orgs_dir
        from .brand import load_brand
        loaders = [("برند (brand.yaml)", lambda: load_brand(self.org, root))]
        from .control.service import load_schedule
        loaders.append(("زمان‌بندی (schedule.yaml)", lambda: load_schedule(Path(root) / self.org / "schedule.yaml")))
        from .agents import marketing as mk
        from .registry import DEFAULT_ROOT, Registry
        if mk.policy_path(self.org, root).is_file():
            loaders.append(("تقویم محتوا و پایگاه دانش (marketing.yaml، knowledge.yaml)",
                            lambda: mk.load_context(self.org, root, Registry.load(DEFAULT_ROOT, check_entrypoints=False))))
        from .crm.store import load_crm
        loaders.append(("CRM (crm.yaml)", lambda: load_crm(self.org, root)))
        from .agents.research import load_research
        loaders.append(("منبع‌های خبری (research.yaml)", lambda: load_research(self.org, root)))
        from .agents.reporting import load_weekly
        loaders.append(("گزارش هفتگی (reporting.yaml)", lambda: load_weekly(self.org, root)))
        for name, load in loaders:
            try:
                load()
                self.add("تنظیمات", name, OK)
            except Exception as e:  # noqa: BLE001
                self.add("تنظیمات", name, FAIL, str(e)[:300], "فایل را با Notepad باز کنید و خطای گفته‌شده را درست کنید.")
        if self.runtime_error:
            self.add("تنظیمات", "فایل .env", FAIL, self.runtime_error[:300], "مقدار گفته‌شده را در .env درست کنید (setup.bat هم می‌پرسد).")

    # ------------------------------------------------------------------ the model

    async def model(self) -> None:
        if self.rtm is None or self.rtm.gateway is None:
            self.add("مدل زبانی", "پاسخ مدل", FAIL, "تنظیمات مدل کامل نیست", "ORCHESTRA_LLM_API_KEY و ORCHESTRA_LLM_MODEL را در .env بنویسید.")
            return
        try:
            text = await self.rtm.gateway.complete(org_id=self.org, run_id=f"check:{self.now.isoformat()}", step_key="check",
                                                   tier="writer", system="فقط یک کلمه جواب بده.", user="بنویس: سلام", max_tokens=20)
            self.add("مدل زبانی", f"پاسخ مدل ({self.cfg.llm_model})", OK, text.strip()[:30])
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            fix = ("اگر سرویس مدل از ایران بسته است، VPN را روشن کنید." if "network" in msg or "وصل نشد" in msg else
                   "کلید یا نام مدل را در .env بررسی کنید." if any(c in msg for c in ("401", "403", "404")) else
                   "سهمیهٔ سرویس مدل تمام شده یا سرویس شلوغ است؛ کمی بعد دوباره امتحان کنید." if "429" in msg else
                   "متن خطا را برای پشتیبانی بفرستید.")
            self.add("مدل زبانی", f"پاسخ مدل ({self.cfg.llm_model})", FAIL, msg[:300], fix)

    # ------------------------------------------------------------------ bots and channels

    def _me(self, platform: str, token: str):
        api = self.bot_factory(platform, token)
        return api, api.call("getMe")

    def bots(self) -> None:
        cfg = self.cfg
        if not cfg.control_bot_token:
            self.add("ربات‌ها", "ربات کنترل", WARN, "توکن ندارد", "ORCHESTRA_CONTROL_BOT_TOKEN را در .env بنویسید (راهنما در README).")
        else:
            try:
                _, me = self._me(cfg.control_platform, cfg.control_bot_token)
                owners = [x for x in cfg.control_chat_ids.split(",") if x.strip()]
                self.add("ربات‌ها", f"ربات کنترل ({cfg.control_platform})", OK if owners else WARN, f"@{me.get('username', '')}",
                         "" if owners else "به ربات پیام بدهید تا شناسه‌تان را بگوید و آن را در ORCHESTRA_CONTROL_CHAT_IDS بنویسید.")
            except Exception as e:  # noqa: BLE001
                self.add("ربات‌ها", f"ربات کنترل ({cfg.control_platform})", FAIL, str(e)[:200], _bot_fix(cfg.control_platform, e))
        from .crm.store import CrmError, load_crm
        try:
            crm_cfg = load_crm(self.org, cfg.orgs_dir)
        except CrmError:
            crm_cfg = None
        if crm_cfg is not None:
            token = os.environ.get(crm_cfg.intake.token_env, "")
            if not token:
                self.add("ربات‌ها", "ربات پذیرش (CRM)", WARN, "خاموش (توکن ندارد)",
                         f"اگر سرنخ می‌خواهید، ربات تازه‌ای در {crm_cfg.intake.platform} بسازید و توکنش را در {crm_cfg.intake.token_env} بنویسید.")
            elif token == cfg.control_bot_token:
                self.add("ربات‌ها", "ربات پذیرش (CRM)", FAIL, "همان توکن ربات کنترل است", "یک ربات جدا بسازید.")
            else:
                try:
                    _, me = self._me(crm_cfg.intake.platform, token)
                    self.add("ربات‌ها", f"ربات پذیرش (CRM، {crm_cfg.intake.platform})", OK, f"@{me.get('username', '')}")
                except Exception as e:  # noqa: BLE001
                    self.add("ربات‌ها", "ربات پذیرش (CRM)", FAIL, str(e)[:200], _bot_fix(crm_cfg.intake.platform, e))

    def channels(self) -> None:
        if not self.destinations:
            self.add("کانال‌ها", "مقصدها", FAIL, "هیچ مقصدی ثبت نشده", "orgs/<org>/destinations.yaml را پر کنید.")
        for d in self.destinations:
            name = f"{d.id} ({d.platform})"
            token = os.environ.get(d.secret_env, "")
            if not d.enabled:
                continue
            if not token:
                self.add("کانال‌ها", name, WARN, f"توکن {d.secret_env} در .env نیست؛ به این مقصد چیزی فرستاده نمی‌شود",
                         f"{d.secret_env} را در .env بنویسید، یا این مقصد را در destinations.yaml با enabled: false خاموش کنید.")
                continue
            if d.platform == "instagram":
                self.instagram(d, token, name)
                continue
            try:
                api, me = self._me(d.platform, token)
                chat = api.call("getChat", {"chat_id": d.chat_id})
                member = api.call("getChatMember", {"chat_id": d.chat_id, "user_id": me["id"]})
            except Exception as e:  # noqa: BLE001
                text = str(e)
                fix = ("ربات را به کانال اضافه کنید و ادمین کنید؛ chat_id در destinations.yaml را هم بررسی کنید."
                       if "not found" in text.lower() or "400" in text else _bot_fix(d.platform, e))
                self.add("کانال‌ها", name, FAIL, text[:200], fix)
                continue
            status = member.get("status")
            can_post = member.get("can_post_messages", True) if chat.get("type") == "channel" else True
            title = chat.get("title") or chat.get("username") or d.chat_id
            if status in ("administrator", "creator") and can_post is not False:
                self.add("کانال‌ها", name, OK, f"ربات در «{title}» اجازهٔ انتشار دارد")
            else:
                self.add("کانال‌ها", name, FAIL, f"ربات در «{title}» ادمین با اجازهٔ ارسال پیام نیست (وضعیت: {status})",
                         "در تنظیمات کانال، ربات را Admin کنید و اجازهٔ «Post messages» را روشن کنید.")

    def instagram(self, d, token: str, name: str) -> None:
        cfg = self.cfg
        if not (cfg.public_asset_dir and cfg.public_asset_url):
            self.add("کانال‌ها", name, FAIL, "محل عمومی تصویرها تنظیم نشده",
                     "اینستاگرام تصویر را از نشانی عمومی برمی‌دارد: ORCHESTRA_PUBLIC_ASSET_DIR و ORCHESTRA_PUBLIC_ASSET_URL (README).")
            return
        import httpx

        from .publishing.instagram import GRAPH
        try:
            r = httpx.get(f"{GRAPH}/{d.chat_id}", params={"fields": "username", "access_token": token}, timeout=20)
            body = r.json() if r.content else {}
        except Exception as e:  # noqa: BLE001
            self.add("کانال‌ها", name, FAIL, f"{type(e).__name__}", "اینترنت یا VPN را بررسی کنید.")
            return
        if r.status_code == 200 and body.get("username"):
            self.add("کانال‌ها", name, OK, f"@{body['username']}")
        else:
            self.add("کانال‌ها", name, FAIL, str((body.get("error") or {}).get("message") or r.status_code)[:200],
                     "توکن بلندمدت با مجوز instagram_content_publish و شناسهٔ حساب Business را بررسی کنید.")

    # ------------------------------------------------------------------ data

    async def sources(self) -> None:
        if self.rtm is None:
            return
        for sid, src in self.rtm.sources.items():
            try:
                parsed = src.parse(await self.rtm.fetcher(src.url))
                self.add("منبع‌های داده", sid, OK if parsed.points else WARN, f"{len(parsed.points)} عدد خوانده شد"
                         + (f"؛ {parsed.warnings[0]}" if parsed.warnings else ""))
            except Exception as e:  # noqa: BLE001
                self.add("منبع‌های داده", sid, FAIL, str(e)[:200],
                         "این سایت‌ها ایرانی‌اند و بدون VPN باز می‌شوند؛ اینترنت را بررسی کنید. اگر ادامه داشت، capture_sources.bat را بفرستید.")

    async def news(self) -> None:
        if self.rtm is None:
            return
        from .agents.research import ResearchError, load_research
        from .data.sources import news
        try:
            cfg = load_research(self.org, self.cfg.orgs_dir)
        except ResearchError:
            return  # reported with the files
        if cfg is None:
            return
        fetch = self.rtm.news_fetcher or (lambda url, foreign: self.rtm.fetcher(url))
        for src in cfg.sources:
            if not src.enabled:
                continue
            try:
                raw = await fetch(src.address, src.foreign)
                items = news.parse_feed(raw) if src.type == "rss" else news.parse_telegram(raw, src.min_chars)
                self.add("منبع‌های خبری", src.name, OK if items else WARN, f"{len(items)} خبر")
            except Exception as e:  # noqa: BLE001
                fix = ("منبع خارجی است: VPN را روشن کنید؛ اگر روشن است، capture_sources.bat خط ORCHESTRA_FOREIGN_PROXY را پیدا می‌کند."
                       if src.foreign else "نشانی فید را بررسی کنید، یا در research.yaml این منبع را enabled: false کنید.")
                self.add("منبع‌های خبری", src.name, FAIL if not src.foreign else WARN, str(e)[:150], fix)

    async def slides(self) -> None:
        try:
            name = await self.browser_probe()
            self.add("اسلایدها", "مرورگر برای ساخت اسلاید", OK, name)
        except Exception as e:  # noqa: BLE001
            self.add("اسلایدها", "مرورگر برای ساخت اسلاید", FAIL, str(e)[:200],
                     "Microsoft Edge یا Chrome را نصب کنید، یا setup.bat را دوباره اجرا کنید.")

    # ------------------------------------------------------------------ backups and disk

    def backup(self) -> None:
        from .backup import same_device
        from .db import ControlState
        bdir = self.cfg.backup_dir if Path(self.cfg.backup_dir).is_absolute() else self.project / self.cfg.backup_dir
        if same_device(bdir, self.project):
            self.add("پشتیبان", "جای پشتیبان", WARN, f"{bdir} روی همان دیسک برنامه است",
                     "در .env بنویسید ORCHESTRA_BACKUP_DIR=... روی دیسک یا فلش دیگر، یا پوشهٔ OneDrive/Google Drive.")
        else:
            self.add("پشتیبان", "جای پشتیبان", OK, str(bdir))
        with self.sessions() as s:
            row = s.get(ControlState, "backup:last")
        if row is None:
            self.add("پشتیبان", "آخرین پشتیبان", WARN, "هنوز پشتیبانی گرفته نشده", "backup.bat را یک بار اجرا کنید.")
        else:
            age = self.now - datetime.fromisoformat(row.value)
            self.add("پشتیبان", "آخرین پشتیبان", OK if age < timedelta(hours=36) else WARN,
                     f"{int(age.total_seconds() // 3600)} ساعت پیش",
                     "" if age < timedelta(hours=36) else "ربات کنترل شب‌ها روشن نبوده؛ backup.bat را اجرا کنید.")
        free = shutil.disk_usage(self.project).free
        self.add("پشتیبان", "فضای خالی دیسک", OK if free > 2e9 else WARN, f"{free / 1e9:.1f} گیگابایت",
                 "" if free > 2e9 else "فضای دیسک کم است؛ فایل‌های اضافه را پاک کنید.")


def _bot(platform: str, token: str):
    from .control.botapi import BotApi
    return BotApi(platform, token, timeout=20)


def _bot_fix(platform: str, e: Exception) -> str:
    text = str(e)
    if "401" in text or "Unauthorized" in text or "404" in text:
        return "توکن را از @BotFather دوباره بگیرید و در .env بنویسید."
    if platform == "telegram":
        return "تلگرام از ایران بدون VPN باز نمی‌شود: VPN را روشن کنید (یا ربات را در بله بسازید)."
    return "اینترنت را بررسی کنید."


async def _browser() -> str:
    from playwright.async_api import async_playwright

    from .visual.render import _launch
    pw = await async_playwright().start()
    try:
        browser = await _launch(pw)
        name = f"{browser.browser_type.name} {browser.version}"
        await browser.close()
        return name
    finally:
        await pw.stop()


def report(checks: list[Check]) -> str:
    counts = {s: sum(c.status == s for c in checks) for s in (OK, WARN, FAIL)}
    lines = [f"🩺 بررسی سلامت: ✅ {counts[OK]} درست، ⚠️ {counts[WARN]} هشدار، ❌ {counts[FAIL]} مشکل"]
    area = None
    for c in sorted(checks, key=lambda c: {FAIL: 0, WARN: 1, OK: 2}[c.status]):
        if c.status == OK and area is None:
            area = "ok"
            lines.append("")
            lines.append("درست‌ها:")
        prefix = f"{ICON[c.status]} [{c.area}] {c.name}"
        lines.append(prefix + (f" — {c.detail}" if c.detail else ""))
        if c.fix and c.status != OK:
            lines.append(f"   ← {c.fix}")
    return "\n".join(lines)


def run_checks(cfg, org: str, *, sessions, rtm=None, runtime_error: str = "", destinations=()) -> str:
    return report(asyncio.run(Doctor(cfg, org, sessions=sessions, rtm=rtm, runtime_error=runtime_error,
                                     destinations=destinations).run()))
