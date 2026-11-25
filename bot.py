"""
VideoEarn - Telegram bot + Mini App backend.

Ikki rejim:
  1) Oddiy rejim  - bot videoni chatga yuboradi, "Ko'rdim" tugmasi vaqt o'tgach ishlaydi.
  2) Mini App     - to'liq VidiPay uslubidagi ilova, ko'rish vaqti server tomonida tekshiriladi.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import sqlite3
import time
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, WebAppInfo)
from aiohttp import web

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------- config
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")  # https://sizning-domen.uz
PORT = int(os.getenv("PORT", "8080"))
RATE_PER_HOUR = float(os.getenv("RATE_PER_HOUR", "0.10"))  # $ / soat
REF_BONUS = float(os.getenv("REF_BONUS", "0.50"))  # do'st 10 daqiqa ko'rsa
MIN_WITHDRAW = float(os.getenv("MIN_WITHDRAW", "5"))
DAILY_CAP_SEC = int(os.getenv("DAILY_CAP_MIN", "120")) * 60
M = 1_000_000  # balans mikro-dollarda saqlanadi (float xatolarsiz)
RATE_MICRO_SEC = RATE_PER_HOUR * M / 3600

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------------------------------------------------------------- database
db = sqlite3.connect("videoearn.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT,
  balance INTEGER DEFAULT 0, bonus INTEGER DEFAULT 0,
  watch_sec INTEGER DEFAULT 0, today_sec INTEGER DEFAULT 0, today_date TEXT,
  ref_by INTEGER, ref_paid INTEGER DEFAULT 0,
  last_hb REAL DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS videos(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, url TEXT, file_id TEXT,
  duration INTEGER DEFAULT 60, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS withdrawals(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
  wallet TEXT, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS views(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, video_id INTEGER,
  sent_at REAL, done INTEGER DEFAULT 0);
"""
)


def money(micro: int) -> str:
    return f"{micro / M:.2f}$"


def get_user(uid):
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def ensure_user(uid, username=None, ref=None):
    if get_user(uid):
        return
    if not ref or ref == uid or not get_user(ref):
        ref = None
    db.execute(
        "INSERT INTO users(id,username,ref_by,created) VALUES(?,?,?,?)",
        (uid, username, ref, int(time.time())),
    )
    db.commit()


def today_seconds(u) -> int:
    return u["today_sec"] if u["today_date"] == date.today().isoformat() else 0


def credit(uid: int, sec: int) -> int:
    """Ko'rish vaqtini hisoblaydi (kunlik limit bilan). Haqiqatda yozilgan soniyani qaytaradi."""
    u = get_user(uid)
    ts = today_seconds(u)
    sec = max(0, min(sec, DAILY_CAP_SEC - ts))
    if sec <= 0:
        return 0
    db.execute(
        "UPDATE users SET balance=balance+?, watch_sec=watch_sec+?, today_sec=?, today_date=? WHERE id=?",
        (int(sec * RATE_MICRO_SEC), sec, ts + sec, date.today().isoformat(), uid),
    )
    u = get_user(uid)
    # Referal bonus: do'st haqiqatan 10 daqiqa ko'rgandan keyin beriladi (soxta akkauntlarga qarshi)
    if u["ref_by"] and not u["ref_paid"] and u["watch_sec"] >= 600:
        db.execute("UPDATE users SET bonus=bonus+? WHERE id=?", (int(REF_BONUS * M), u["ref_by"]))
        db.execute("UPDATE users SET ref_paid=1 WHERE id=?", (uid,))
    db.commit()
    return sec


def create_withdrawal(uid: int, amount: float, wallet: str):
    amt = int(amount * M)
    u = get_user(uid)
    if amount < MIN_WITHDRAW:
        return None, f"Minimal summa: {MIN_WITHDRAW:.2f}$"
    if len(wallet.strip()) < 6:
        return None, "Hamyon/karta raqamini to'g'ri kiriting"
    if u["balance"] + u["bonus"] < amt:
        return None, "Balans yetarli emas"
    from_bal = min(u["balance"], amt)
    db.execute(
        "UPDATE users SET balance=balance-?, bonus=bonus-? WHERE id=?",
        (from_bal, amt - from_bal, uid),
    )
    cur = db.execute(
        "INSERT INTO withdrawals(user_id,amount,wallet,status,created) VALUES(?,?,?,?,?)",
        (uid, amt, wallet.strip(), "pending", int(time.time())),
    )
    db.commit()
    return cur.lastrowid, None


async def notify_admins(wid: int, uid: int, amount: int, wallet: str):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data=f"wd:ok:{wid}"),
            InlineKeyboardButton(text="❌ Rad etish", callback_data=f"wd:no:{wid}"),
        ]]
    )
    text = f"💸 <b>Yechish so'rovi #{wid}</b>\nFoydalanuvchi: <code>{uid}</code>\nSumma: {money(amount)}\nHamyon: <code>{wallet}</code>"
    for a in ADMINS:
        try:
            await bot.send_message(a, text, reply_markup=kb)
        except Exception:
            logging.exception("admin notify")


# ---------------------------------------------------------------- bot (oddiy rejim)
def main_kb() -> InlineKeyboardMarkup:
    rows = []
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="🚀 Ilovani ochish", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([InlineKeyboardButton(text="📺 Video ko'rish (oddiy rejim)", callback_data="watch")])
    rows.append([
        InlineKeyboardButton(text="💰 Balans", callback_data="bal"),
        InlineKeyboardButton(text="👥 Do'stlar", callback_data="ref"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def balance_text(uid: int) -> str:
    u = get_user(uid)
    left = max(0, DAILY_CAP_SEC - today_seconds(u)) // 60
    return (
        f"💰 <b>Asosiy balans:</b> {money(u['balance'])}\n"
        f"🎁 <b>Bonus:</b> {money(u['bonus'])}\n"
        f"⏱ Jami ko'rilgan: {u['watch_sec'] // 60} daqiqa\n"
        f"📅 Bugun qolgan limit: {left} daqiqa\n\n"
        f"Yechish: <code>/withdraw 10 KARTA_YOKI_HAMYON</code> (min {MIN_WITHDRAW:.0f}$)"
    )


@router.message(CommandStart())
async def start(m: Message, command: CommandObject):
    ref = int(command.args) if command.args and command.args.isdigit() else None
    ensure_user(m.from_user.id, m.from_user.username, ref)
    await m.answer("Salom! Video ko'ring va balansingizni oshiring.", reply_markup=main_kb())


@router.callback_query(F.data == "bal")
async def cb_bal(c: CallbackQuery):
    ensure_user(c.from_user.id, c.from_user.username)
    await c.message.answer(balance_text(c.from_user.id))
    await c.answer()


@router.message(Command("balance"))
async def cmd_bal(m: Message):
    ensure_user(m.from_user.id, m.from_user.username)
    await m.answer(balance_text(m.from_user.id))


@router.callback_query(F.data == "ref")
async def cb_ref(c: CallbackQuery):
    me = await bot.get_me()
    r = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(ref_paid),0) p FROM users WHERE ref_by=?", (c.from_user.id,)
    ).fetchone()
    await c.message.answer(
        f"👥 Taklif qilinganlar: {r['n']} (faol: {r['p']})\n"
        f"Har bir faol do'st uchun: {REF_BONUS:.2f}$ bonus\n\n"
        f"Havolangiz:\nhttps://t.me/{me.username}?start={c.from_user.id}"
    )
    await c.answer()


@router.callback_query(F.data == "watch")
async def cb_watch(c: CallbackQuery):
    ensure_user(c.from_user.id, c.from_user.username)
    u = get_user(c.from_user.id)
    if today_seconds(u) >= DAILY_CAP_SEC:
        return await c.answer("Bugungi limit tugadi. Ertaga qaytib keling.", show_alert=True)
    vids = db.execute("SELECT * FROM videos WHERE active=1").fetchall()
    if not vids:
        return await c.answer("Hozircha video yo'q.", show_alert=True)
    v = random.choice(vids)
    cur = db.execute(
        "INSERT INTO views(user_id,video_id,sent_at) VALUES(?,?,?)", (c.from_user.id, v["id"], time.time())
    )
    db.commit()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Ko'rdim", callback_data=f"done:{cur.lastrowid}")]]
    )
    caption = f"{v['title'] or 'Video'}\nTo'liq ko'ring ({v['duration']} soniya), keyin «Ko'rdim» ni bosing."
    src = v["file_id"] or v["url"]
    await c.message.answer_video(src, caption=caption, reply_markup=kb, protect_content=True)
    await c.answer()


@router.callback_query(F.data.startswith("done:"))
async def cb_done(c: CallbackQuery):
    vid = int(c.data.split(":")[1])
    row = db.execute(
        "SELECT w.*, v.duration FROM views w JOIN videos v ON v.id=w.video_id WHERE w.id=? AND w.user_id=?",
        (vid, c.from_user.id),
    ).fetchone()
    if not row or row["done"]:
        return await c.answer("Bu video allaqachon hisoblangan.", show_alert=True)
    elapsed = time.time() - row["sent_at"]
    if elapsed < row["duration"] * 0.9:
        return await c.answer(f"Hali erta. Yana {int(row['duration'] - elapsed)} soniya kuting.", show_alert=True)
    db.execute("UPDATE views SET done=1 WHERE id=?", (vid,))
    db.commit()
    got = credit(c.from_user.id, row["duration"])
    await c.answer(f"+{got * RATE_MICRO_SEC / M:.3f}$ hisoblandi" if got else "Kunlik limit tugagan", show_alert=True)
    await c.message.edit_reply_markup(reply_markup=None)


@router.message(Command("withdraw"))
async def cmd_withdraw(m: Message, command: CommandObject):
    ensure_user(m.from_user.id, m.from_user.username)
    parts = (command.args or "").split(maxsplit=1)
    try:
        amount, wallet = float(parts[0]), parts[1]
    except (ValueError, IndexError):
        return await m.answer("Format: <code>/withdraw 10 KARTA_YOKI_HAMYON</code>")
    wid, err = create_withdrawal(m.from_user.id, amount, wallet)
    if err:
        return await m.answer(f"❌ {err}")
    await m.answer(f"✅ So'rov #{wid} qabul qilindi. Admin ko'rib chiqadi.")
    await notify_admins(wid, m.from_user.id, int(amount * M), wallet)


@router.callback_query(F.data.startswith("wd:"))
async def cb_wd(c: CallbackQuery):
    if c.from_user.id not in ADMINS:
        return await c.answer("Ruxsat yo'q", show_alert=True)
    _, action, wid = c.data.split(":")
    w = db.execute("SELECT * FROM withdrawals WHERE id=?", (int(wid),)).fetchone()
    if not w or w["status"] != "pending":
        return await c.answer("Allaqachon ko'rib chiqilgan", show_alert=True)
    if action == "ok":
        db.execute("UPDATE withdrawals SET status='accepted' WHERE id=?", (w["id"],))
        msg = f"✅ So'rov #{w['id']} ({money(w['amount'])}) tasdiqlandi."
    else:
        db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (w["id"],))
        db.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
        msg = f"❌ So'rov #{w['id']} rad etildi, mablag' balansga qaytarildi."
    db.commit()
    try:
        await bot.send_message(w["user_id"], msg)
    except Exception:
        pass
    await c.message.edit_text(c.message.text + f"\n\n→ {action.upper()}")
    await c.answer()


# ---------------------------------------------------------------- admin
def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMINS


@router.message(F.video, F.from_user.id.in_(ADMINS))
async def admin_video(m: Message):
    """Videoni botga yuboring. Izoh: 'Sarlavha | soniya' (ixtiyoriy)."""
    title, dur = (m.caption or "Video"), m.video.duration or 60
    if "|" in title:
        title, d = [x.strip() for x in title.split("|", 1)]
        dur = int(d) if d.isdigit() else dur
    db.execute(
        "INSERT INTO videos(title,file_id,duration) VALUES(?,?,?)", (title, m.video.file_id, dur)
    )
    db.commit()
    await m.answer("✅ Video qo'shildi (faqat oddiy rejim uchun). Mini App uchun /addvideo bilan URL qo'shing.")


@router.message(Command("addvideo"))
async def admin_addvideo(m: Message, command: CommandObject):
    if not is_admin(m):
        return
    try:
        dur, url, title = (command.args or "").split(maxsplit=2)
        dur = int(dur)
    except ValueError:
        return await m.answer("Format: <code>/addvideo 60 https://site.uz/v.mp4 Sarlavha</code>")
    db.execute("INSERT INTO videos(title,url,duration) VALUES(?,?,?)", (title, url, dur))
    db.commit()
    await m.answer("✅ Qo'shildi (oddiy rejim va Mini App ikkalasida ishlaydi).")


@router.message(Command("videos"))
async def admin_videos(m: Message):
    if not is_admin(m):
        return
    rows = db.execute("SELECT * FROM videos").fetchall()
    await m.answer("\n".join(
        f"#{v['id']} {v['title']} ({v['duration']}s) {'URL' if v['url'] else 'FILE'} {'on' if v['active'] else 'off'}"
        for v in rows) or "Bo'sh")


@router.message(Command("delvideo"))
async def admin_delvideo(m: Message, command: CommandObject):
    if is_admin(m) and (command.args or "").isdigit():
        db.execute("DELETE FROM videos WHERE id=?", (int(command.args),))
        db.commit()
        await m.answer("🗑 O'chirildi")


@router.message(Command("stats"))
async def admin_stats(m: Message):
    if not is_admin(m):
        return
    n = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    p = db.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'").fetchone()
    await m.answer(f"👤 Foydalanuvchilar: {n}\n⏳ Kutilayotgan yechishlar: {p[0]} ({money(p[1])})")


# ---------------------------------------------------------------- Mini App API
def check_init_data(init_data: str):
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", None)
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not got or not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 86400:
        return None
    return json.loads(data["user"])


async def auth(request):
    body = await request.json()
    user = check_init_data(body.get("initData", ""))
    if not user:
        raise web.HTTPUnauthorized()
    ensure_user(user["id"], user.get("username"))
    return user["id"], body


def state(uid: int) -> dict:
    u = get_user(uid)
    r = db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(ref_paid),0) p FROM users WHERE ref_by=?", (uid,)
    ).fetchone()
    vids = [
        {"id": v["id"], "title": v["title"], "url": v["url"]}
        for v in db.execute("SELECT * FROM videos WHERE active=1 AND url IS NOT NULL")
    ]
    hist = [
        {"id": w["id"], "amount": w["amount"] / M, "status": w["status"], "created": w["created"]}
        for w in db.execute("SELECT * FROM withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 30", (uid,))
    ]
    return {
        "balance": u["balance"] / M, "bonus": u["bonus"] / M,
        "watch_sec": u["watch_sec"], "today_sec": today_seconds(u), "cap_sec": DAILY_CAP_SEC,
        "rate_hour": RATE_PER_HOUR, "ref_bonus": REF_BONUS, "min_withdraw": MIN_WITHDRAW,
        "refs": r["n"], "refs_active": r["p"], "videos": vids, "history": hist,
        "ref_link": f"https://t.me/{BOT_USERNAME}?start={uid}",
    }


async def h_state(request):
    uid, _ = await auth(request)
    return web.json_response(state(uid))


async def h_heartbeat(request):
    """Mijoz video ko'rilayotganda har 5 soniyada chaqiradi. Server vaqtni o'zi o'lchaydi."""
    uid, _ = await auth(request)
    now = time.time()
    delta = now - (get_user(uid)["last_hb"] or 0)
    credited = 0
    if delta >= 4:  # juda tez-tez yuborilsa (bir nechta tab / skript) hisoblanmaydi
        if delta <= 12:
            credited = credit(uid, int(min(delta, 7)))
        db.execute("UPDATE users SET last_hb=? WHERE id=?", (now, uid))
        db.commit()
    return web.json_response({**state(uid), "credited": credited})


async def h_withdraw(request):
    uid, body = await auth(request)
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        return web.json_response({"error": "Summa noto'g'ri"}, status=400)
    wallet = str(body.get("wallet", ""))[:100]
    wid, err = create_withdrawal(uid, amount, wallet)
    if err:
        return web.json_response({"error": err}, status=400)
    await notify_admins(wid, uid, int(amount * M), wallet)
    return web.json_response(state(uid))


async def h_index(request):
    return web.FileResponse(Path(__file__).parent / "webapp" / "index.html")


BOT_USERNAME = ""


async def main():
    global BOT_USERNAME
    BOT_USERNAME = (await bot.get_me()).username
    app = web.Application()
    app.add_routes([
        web.get("/", h_index),
        web.post("/api/state", h_state),
        web.post("/api/heartbeat", h_heartbeat),
        web.post("/api/withdraw", h_withdraw),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Web server: port %s", PORT)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
