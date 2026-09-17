"""
KrishManager Pro
Production-style multi-bot Telegram manager foundation.

Environment:
  DATABASE_URL
  MANAGER_BOT_TOKEN
  ADMIN_ID
  ADMIN_USERNAME
  MANAGER_NAME (optional)
  TOKEN_ENCRYPTION_KEY (optional Fernet key; generated key is printed if absent)

The database is additive: no DROP/TRUNCATE commands are used.
"""

import os, time, json, logging, threading, secrets, hashlib, base64
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from cryptography.fernet import Fernet
import telebot
from telebot import types

# ---------------- CONFIG ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("KrishManager")

DATABASE_URL = os.environ["DATABASE_URL"]
MANAGER_BOT_TOKEN = os.environ["MANAGER_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").lstrip("@")
MANAGER_NAME = os.getenv("MANAGER_NAME", "KrishManager")

key = os.getenv("TOKEN_ENCRYPTION_KEY")
if not key:
    raise RuntimeError("TOKEN_ENCRYPTION_KEY is required. Generate a Fernet key once and store it permanently in Railway Variables.")
CIPHER = Fernet(key.encode())

manager = telebot.TeleBot(MANAGER_BOT_TOKEN, parse_mode="HTML")
DB = pool.ThreadedConnectionPool(1, 20, DATABASE_URL, sslmode="require")

# In-memory managed bot instances. Tokens are decrypted only when needed.
MANAGED = {}
STATE = {}
LOCK = threading.RLock()

# ---------------- DATABASE ----------------
def db(sql, args=(), fetch=False, one=False):
    conn = DB.getconn()
    try:
        with conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, args)
                if fetch:
                    rows = cur.fetchall()
                    if one:
                        return rows[0] if rows else None
                    return rows
                return None
    finally:
        DB.putconn(conn)

def init_db():
    db("""
    CREATE TABLE IF NOT EXISTS manager_users(
      user_id BIGINT PRIMARY KEY,
      username TEXT,
      first_name TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS managed_bots(
      id BIGSERIAL PRIMARY KEY,
      owner_id BIGINT NOT NULL REFERENCES manager_users(user_id) ON DELETE CASCADE,
      bot_id BIGINT NOT NULL,
      bot_username TEXT,
      bot_name TEXT,
      token_ciphertext TEXT NOT NULL,
      active BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(owner_id, bot_id)
    );

    CREATE TABLE IF NOT EXISTS bot_settings(
      bot_id BIGINT PRIMARY KEY REFERENCES managed_bots(id) ON DELETE CASCADE,
      start_text TEXT NOT NULL DEFAULT '👋 Welcome to {first_name}!',
      start_media_type TEXT,
      start_media_id TEXT,
      watermark_enabled BOOLEAN NOT NULL DEFAULT TRUE,
      watermark_text TEXT NOT NULL DEFAULT '⚙️ Managed via @YourManagerBot',
      support_username TEXT,
      news_channel TEXT,
      privacy_policy TEXT,
      language TEXT NOT NULL DEFAULT 'English',
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS bot_channels(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      channel_id TEXT NOT NULL,
      title TEXT NOT NULL DEFAULT 'Channel',
      link TEXT,
      button_label TEXT NOT NULL DEFAULT '📢 Join Channel',
      button_style TEXT NOT NULL DEFAULT '#b',
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(bot_id, channel_id)
    );

    CREATE TABLE IF NOT EXISTS bot_users(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      username TEXT,
      first_name TEXT,
      last_name TEXT,
      status TEXT NOT NULL DEFAULT 'active',
      joined_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      last_seen TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(bot_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS force_join_requests(
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      channel_id TEXT NOT NULL,
      requested_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY(bot_id,user_id,channel_id)
    );

    CREATE TABLE IF NOT EXISTS auto_dm_rules(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      trigger_type TEXT NOT NULL,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      text TEXT NOT NULL DEFAULT '👋 Welcome {first_name}!',
      media_type TEXT,
      media_id TEXT,
      buttons_json TEXT NOT NULL DEFAULT '[]',
      delay_seconds INTEGER NOT NULL DEFAULT 0,
      UNIQUE(bot_id,trigger_type)
    );

    CREATE TABLE IF NOT EXISTS broadcasts(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      text TEXT,
      media_type TEXT,
      media_id TEXT,
      buttons_json TEXT NOT NULL DEFAULT '[]',
      pin_message BOOLEAN NOT NULL DEFAULT FALSE,
      created_by BIGINT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS broadcast_logs(
      id BIGSERIAL PRIMARY KEY,
      broadcast_id BIGINT NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      status TEXT NOT NULL,
      error TEXT,
      sent_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(broadcast_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS membership_plans(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      name TEXT NOT NULL,
      price NUMERIC(12,2) NOT NULL DEFAULT 0,
      duration_days INTEGER NOT NULL DEFAULT 30,
      description TEXT DEFAULT '',
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS memberships(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      plan_id BIGINT REFERENCES membership_plans(id) ON DELETE SET NULL,
      expires_at TIMESTAMPTZ NOT NULL,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS support_map(
      admin_message_id BIGINT PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS tutorials(
      id BIGSERIAL PRIMARY KEY,
      feature_key TEXT UNIQUE NOT NULL,
      title TEXT NOT NULL,
      body TEXT NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS audit_logs(
      id BIGSERIAL PRIMARY KEY,
      actor_id BIGINT NOT NULL,
      bot_id BIGINT,
      action TEXT NOT NULL,
      target_user_id BIGINT,
      details TEXT DEFAULT '',
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );


    CREATE TABLE IF NOT EXISTS bot_buttons(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      scope TEXT NOT NULL,
      label TEXT NOT NULL,
      url TEXT,
      callback_data TEXT,
      row_no INTEGER NOT NULL DEFAULT 0,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      UNIQUE(bot_id,scope,row_no,label)
    );
    CREATE TABLE IF NOT EXISTS user_notes(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      note TEXT NOT NULL,
      created_by BIGINT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS membership_events(
      id BIGSERIAL PRIMARY KEY,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      action TEXT NOT NULL,
      plan_id BIGINT REFERENCES membership_plans(id) ON DELETE SET NULL,
      days INTEGER,
      actor_id BIGINT NOT NULL,
      details TEXT DEFAULT '',
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS dm_logs(
      id BIGSERIAL PRIMARY KEY,
      rule_id BIGINT REFERENCES auto_dm_rules(id) ON DELETE CASCADE,
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      user_id BIGINT NOT NULL,
      status TEXT NOT NULL,
      error TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS bot_feature_flags(
      bot_id BIGINT NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
      feature_key TEXT NOT NULL,
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      PRIMARY KEY(bot_id,feature_key)
    );
    CREATE INDEX IF NOT EXISTS idx_notes_user ON user_notes(bot_id,user_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_membership_events ON membership_events(bot_id,user_id,created_at);
    CREATE INDEX IF NOT EXISTS idx_dm_logs ON dm_logs(bot_id,user_id,created_at);

    CREATE INDEX IF NOT EXISTS idx_bot_users_bot ON bot_users(bot_id);
    CREATE INDEX IF NOT EXISTS idx_memberships_user ON memberships(bot_id,user_id);
    CREATE INDEX IF NOT EXISTS idx_audit_bot ON audit_logs(bot_id,created_at);
    """)
    defaults = [
      ("connect","🤖 Connect / Manage Bot",
       "Create your Telegram bot with Telegram's official bot-management flow.\\n\\n"
       "Never paste a token into public chats. The manager stores connected credentials encrypted."),
      ("start","🏠 Start Message",
       "Open Start Message → Edit Text. Supported variables:\\n"
       "<code>{first_name}</code> <code>{last_name}</code> <code>{username}</code> <code>{user_id}</code>."),
      ("force_join","📢 Force Join",
       "Add the managed bot as an administrator in the channel.\\n\\n"
       "Command-style example:\\n<code>#g Channel Name -1001234567890 https://t.me/example</code>\\n\\n"
       "#r/#g/#b are internal style labels for red/green/blue buttons. Telegram itself does not support arbitrary HEX button colors."),
      ("broadcast","📣 Broadcast",
       "Create a message, optionally attach photo/video/document and inline buttons, preview it, then send to active users. Delivery is logged per user."),
      ("auto_dm","💬 Auto-DM",
       "Enable Join Request DM and/or New Member DM. Configure separate text/media/buttons and optional delay. "
       "Variables: <code>{first_name}</code>, <code>{username}</code>, <code>{user_id}</code>, <code>{channel_name}</code>."),
      ("channels","📢 Channels",
       "Add, edit, disable or remove configured channels. Each managed bot keeps its own channel list."),
      ("support","💬 Support Inbox",
       "Users can send a message to the managed bot. The manager maps each support message to its sender so an admin reply can be routed back."),
      ("membership","💎 Membership",
       "Admin creates plans with name, price, duration and features. User selects a plan and receives the configured admin contact."),
      ("watermark","💧 Remove Watermark",
       "Watermark is configurable per managed bot. Active membership can suppress the watermark; expired membership can restore it."),
      ("statistics","📊 Statistics",
       "View registered users, active users, channels, membership count and broadcast delivery statistics."),
      ("settings","⚙️ Settings",
       "Configure start message, support username, news channel, privacy policy, language and watermark.")
    ]
    for k,t,b in defaults:
        db("INSERT INTO tutorials(feature_key,title,body) VALUES(%s,%s,%s) ON CONFLICT(feature_key) DO NOTHING",(k,t,b))

def audit(actor, action, bot_id=None, target=None, details=""):
    db("INSERT INTO audit_logs(actor_id,bot_id,action,target_user_id,details) VALUES(%s,%s,%s,%s,%s)",
       (actor,bot_id,action,target,details))

def save_manager_user(u):
    db("""INSERT INTO manager_users(user_id,username,first_name) VALUES(%s,%s,%s)
         ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username,
         first_name=EXCLUDED.first_name,updated_at=CURRENT_TIMESTAMP""",
       (u.id,u.username,u.first_name))

def save_bot_user(bot_pk,u):
    db("""INSERT INTO bot_users(bot_id,user_id,username,first_name,last_name)
         VALUES(%s,%s,%s,%s,%s)
         ON CONFLICT(bot_id,user_id) DO UPDATE SET username=EXCLUDED.username,
         first_name=EXCLUDED.first_name,last_name=EXCLUDED.last_name,last_seen=CURRENT_TIMESTAMP""",
       (bot_pk,u.id,u.username,u.first_name,u.last_name))

# ---------------- UI ----------------
def home_kb(uid):
    k=types.InlineKeyboardMarkup()
    k.row(types.InlineKeyboardButton("🤖 My Bots",callback_data="mbots"))
    k.row(types.InlineKeyboardButton("➕ Connect Bot",callback_data="connect"))
    k.row(types.InlineKeyboardButton("📚 Tutorial Center",callback_data="tutorials"))
    k.row(types.InlineKeyboardButton("💎 Membership Plans",callback_data="plans"))
    k.row(types.InlineKeyboardButton("💧 Remove Watermark",callback_data="plans"))
    k.row(types.InlineKeyboardButton("⚙️ Manager Settings",callback_data="msettings"))
    if uid==ADMIN_ID:
        k.row(types.InlineKeyboardButton("🛠 Master Admin",callback_data="master"))
    return k

def back_home():
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data="home"))
    return k

def manager_text():
    return (f"<b>🤖 {MANAGER_NAME}</b>\\n\\n"
            "A professional multi-bot control center.\\n\\n"
            "Choose a module below.")

def variables(text, u, channel_name=""):
    vals={
      "{first_name}":u.first_name or "",
      "{last_name}":u.last_name or "",
      "{username}":("@"+u.username) if u.username else "",
      "{user_id}":str(u.id),
      "{channel_name}":channel_name or ""
    }
    for a,b in vals.items(): text=text.replace(a,b)
    return text

def add_watermark(text, bot_pk):
    s=db("SELECT watermark_enabled,watermark_text FROM bot_settings WHERE bot_id=%s",(bot_pk,),True)
    if s and s["watermark_enabled"] and s["watermark_text"]:
        return (text or "") + "\\n\\n" + s["watermark_text"]
    return text or ""

def member_active(bot_pk,user_id):
    r=db("""SELECT 1 FROM memberships WHERE bot_id=%s AND user_id=%s
            AND status='active' AND expires_at>CURRENT_TIMESTAMP LIMIT 1""",
         (bot_pk,user_id),True)
    return bool(r)

def safe_username(name):
    return (name or "admin").lstrip("@").replace(" ","")


# ---------------- CORE HELPERS ----------------
def is_master(uid): return int(uid)==ADMIN_ID

def bot_row(pk,uid=None):
    if uid is None: return db("SELECT * FROM managed_bots WHERE id=%s",(pk,),True)
    return db("SELECT * FROM managed_bots WHERE id=%s AND owner_id=%s",(pk,uid),True)

def ensure_owner(pk,uid): return bool(bot_row(pk,uid))

def ensure_settings(pk):
    db("INSERT INTO bot_settings(bot_id,support_username) VALUES(%s,%s) ON CONFLICT(bot_id) DO NOTHING",(pk,ADMIN_USERNAME))
    return db("SELECT * FROM bot_settings WHERE bot_id=%s",(pk,),True)

def feature_enabled(pk,key):
    r=db("SELECT enabled FROM bot_feature_flags WHERE bot_id=%s AND feature_key=%s",(pk,key),True)
    return True if not r else bool(r["enabled"])

def set_feature(pk,key,enabled):
    db("INSERT INTO bot_feature_flags(bot_id,feature_key,enabled) VALUES(%s,%s,%s) ON CONFLICT(bot_id,feature_key) DO UPDATE SET enabled=EXCLUDED.enabled",(pk,key,enabled))

def kb_back(pk):
    k=types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}")); return k

def button_json(label,url=None,callback_data=None):
    return {"label":label,"url":url,"callback_data":callback_data}

def parse_buttons(raw):
    try:
        arr=json.loads(raw or "[]")
        return arr if isinstance(arr,list) else []
    except Exception:return []

def make_markup(arr):
    if not arr:return None
    k=types.InlineKeyboardMarkup(); row=[]
    for b in arr:
        label=str(b.get("label") or "Button")[:64]
        if b.get("url"): btn=types.InlineKeyboardButton(label,url=b["url"])
        elif b.get("callback_data"): btn=types.InlineKeyboardButton(label,callback_data=str(b["callback_data"])[:64])
        else: continue
        row.append(btn)
        if len(row)>=2:k.row(*row); row=[]
    if row:k.row(*row)
    return k

def get_scope_buttons(pk,scope):
    return db("SELECT label,url,callback_data FROM bot_buttons WHERE bot_id=%s AND scope=%s AND enabled=TRUE ORDER BY row_no,id",(pk,scope),True)

def send_with_buttons(inst,user_id,text,media_type=None,media_id=None,buttons=None,pk=None,source_user=None,channel_name=""):
    u=source_user
    if not u:
        class U: pass
        u=U();u.id=user_id;u.first_name="";u.last_name="";u.username=""
    text=variables(text or "",u,channel_name)
    if pk and not member_active(pk,user_id): text=add_watermark(text,pk)
    markup=make_markup(buttons or [])
    if media_type=="photo" and media_id:return inst.send_photo(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="video" and media_id:return inst.send_video(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="document" and media_id:return inst.send_document(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="audio" and media_id:return inst.send_audio(user_id,media_id,caption=text or None,reply_markup=markup)
    return inst.send_message(user_id,text or " ",reply_markup=markup)

def admin_menu_for_bot(pk):
    return bot_panel(pk)

def expire_memberships():
    db("UPDATE memberships SET status='expired',updated_at=CURRENT_TIMESTAMP WHERE status='active' AND expires_at<=CURRENT_TIMESTAMP")

def activate_membership(pk,user_id,plan_id,days,actor,action="grant",details=""):
    expire_memberships()
    p=db("SELECT * FROM membership_plans WHERE id=%s AND bot_id=%s",(plan_id,pk),True)
    if not p:return False
    current=db("SELECT * FROM memberships WHERE bot_id=%s AND user_id=%s AND status='active' AND expires_at>CURRENT_TIMESTAMP ORDER BY expires_at DESC LIMIT 1",(pk,user_id),True)
    base=current["expires_at"] if current else datetime.now(timezone.utc)
    expires=base+timedelta(days=days)
    if current:
        db("UPDATE memberships SET plan_id=%s,expires_at=%s,status='active',updated_at=CURRENT_TIMESTAMP WHERE id=%s",(plan_id,expires,current["id"]))
    else:
        db("INSERT INTO memberships(bot_id,user_id,plan_id,expires_at,status) VALUES(%s,%s,%s,%s,'active')",(pk,user_id,plan_id,expires))
    db("INSERT INTO membership_events(bot_id,user_id,action,plan_id,days,actor_id,details) VALUES(%s,%s,%s,%s,%s,%s,%s)",(pk,user_id,action,plan_id,days,actor,details))
    audit(actor,action,pk,user_id,details)
    return True

def remove_membership(pk,user_id,actor):
    db("UPDATE memberships SET status='revoked',updated_at=CURRENT_TIMESTAMP WHERE bot_id=%s AND user_id=%s AND status='active'",(pk,user_id))
    db("INSERT INTO membership_events(bot_id,user_id,action,actor_id,details) VALUES(%s,%s,'revoke',%s,'membership revoked')",(pk,user_id,actor))
    audit(actor,"revoke_membership",pk,user_id)

def owner_notify(pk,text):
    r=bot_row(pk)
    if r:
        try: manager.send_message(r["owner_id"],text)
        except Exception: pass


# ---------------- MANAGER BOT ----------------
@manager.message_handler(commands=["start"])
def start(m):
    save_manager_user(m.from_user)
    manager.send_message(m.chat.id,manager_text(),reply_markup=home_kb(m.from_user.id))

@manager.callback_query_handler(func=lambda c:c.data=="home")
def cb_home(c):
    manager.edit_message_text(manager_text(),c.message.chat.id,c.message.id,reply_markup=home_kb(c.from_user.id))

@manager.callback_query_handler(func=lambda c:c.data=="connect")
def cb_connect(c):
    STATE[c.from_user.id]={"action":"connect_token"}
    manager.edit_message_text(
      "<b>➕ Connect Managed Bot</b>\\n\\n"
      "Send the bot token using the secure manager connection flow.\\n"
      "For safety, do not share tokens in group chats.\\n\\n"
      "Send /cancel to stop.",
      c.message.chat.id,c.message.id,reply_markup=back_home())

@manager.message_handler(commands=["cancel"])
def cancel(m):
    STATE.pop(m.from_user.id,None)
    manager.send_message(m.chat.id,"❌ Cancelled.",reply_markup=home_kb(m.from_user.id))

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="connect_token",content_types=["text"])
def receive_token(m):
    STATE.pop(m.from_user.id,None)
    token=m.text.strip()
    try:
        try: manager.delete_message(m.chat.id,m.message_id)
        except Exception: pass
        probe=telebot.TeleBot(token)
        me=probe.get_me()
        if not me or not me.is_bot:
            raise ValueError("Not a bot")
        save_manager_user(m.from_user)
        enc=CIPHER.encrypt(token.encode()).decode()
        existing=db("SELECT id FROM managed_bots WHERE owner_id=%s AND bot_id=%s",(m.from_user.id,me.id),True)
        if existing:
            db("UPDATE managed_bots SET token_ciphertext=%s,bot_username=%s,bot_name=%s,active=TRUE,updated_at=CURRENT_TIMESTAMP WHERE id=%s",
               (enc,me.username,me.first_name,existing["id"]))
            bot_pk=existing["id"]
        else:
            row=db("""INSERT INTO managed_bots(owner_id,bot_id,bot_username,bot_name,token_ciphertext)
                      VALUES(%s,%s,%s,%s,%s) RETURNING id""",
                   (m.from_user.id,me.id,me.username,me.first_name,enc),True)
            bot_pk=row["id"]
            db("INSERT INTO bot_settings(bot_id,support_username) VALUES(%s,%s) ON CONFLICT(bot_id) DO NOTHING",
               (bot_pk,ADMIN_USERNAME))
        audit(m.from_user.id,"connect_bot",bot_pk,details=f"@{me.username}")
        start_managed_bot(bot_pk)
        manager.send_message(m.chat.id,
          f"✅ <b>{me.first_name}</b> connected successfully.\\n\\n"
          f"Bot: @{me.username or me.id}\\nID: <code>{me.id}</code>\\n\\n"
          "Use My Bots to open its control panel.",
          reply_markup=home_kb(m.from_user.id))
    except Exception as e:
        log.exception("connect")
        manager.send_message(m.chat.id,f"❌ Could not connect this bot.\\n<code>{str(e)[:500]}</code>")

@manager.callback_query_handler(func=lambda c:c.data=="mbots")
def cb_bots(c):
    if c.from_user.id==ADMIN_ID:
        rows=db("""SELECT b.id,b.bot_username,b.bot_name,b.active,b.owner_id,mu.username owner_username
                   FROM managed_bots b LEFT JOIN manager_users mu ON mu.user_id=b.owner_id
                   ORDER BY b.id DESC""",fetch=True)
    else:
        rows=db("SELECT id,bot_username,bot_name,active,owner_id,NULL owner_username FROM managed_bots WHERE owner_id=%s ORDER BY id DESC",(c.from_user.id,),True)
    k=types.InlineKeyboardMarkup()
    for r in rows:
        owner_hint = f" • @{r['owner_username']}" if c.from_user.id==ADMIN_ID and r.get('owner_username') else ""
        k.add(types.InlineKeyboardButton(
          f"{'🟢' if r['active'] else '🔴'} @{r['bot_username'] or r['bot_name'] or r['id']}{owner_hint}"[:64],
          callback_data=f"bot:{r['id']}"))
    k.add(types.InlineKeyboardButton("➕ Connect Bot",callback_data="connect"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data="home"))
    manager.edit_message_text("<b>🤖 My Managed Bots</b>\\n\\nSelect a bot:",c.message.chat.id,c.message.id,reply_markup=k)

def bot_panel(bot_pk):
    k=types.InlineKeyboardMarkup()
    rows=[
      ("🏠 Start Message","startset"),("📢 Force Join","fj"),("📣 Broadcast","broadcast"),
      ("💬 Auto-DM","autodm"),("💬 Support Inbox","support"),
      ("👥 Users","users"),("📢 Channels","channels"),
      ("💎 Membership","botplans"),("💧 Watermark","watermark"),
      ("📊 Statistics","stats"),("⚙️ Settings","botsettings"),("📚 Tutorials","tutorials"),("⏯ Bot Status","botstatus"),("🗑 Remove Bot","deletebot")
    ]
    for label,code in rows:k.add(types.InlineKeyboardButton(label,callback_data=f"m:{code}:{bot_pk}"))
    k.add(types.InlineKeyboardButton("⬅️ My Bots",callback_data="mbots"))
    return k

@manager.callback_query_handler(func=lambda c:c.data.startswith("bot:"))
def cb_bot(c):
    pk=int(c.data.split(":")[1])
    if c.from_user.id==ADMIN_ID:
        r=db("SELECT bot_username,bot_name,owner_id FROM managed_bots WHERE id=%s",(pk,),True)
    else:
        r=db("SELECT bot_username,bot_name,owner_id FROM managed_bots WHERE id=%s AND owner_id=%s",(pk,c.from_user.id),True)
    if not r:return
    manager.edit_message_text(f"<b>🤖 @{r['bot_username'] or r['bot_name']}</b>\\n\\nSelect a module:",c.message.chat.id,c.message.id,reply_markup=bot_panel(pk))

def owner_of(pk,uid):
    return bool(db("SELECT 1 FROM managed_bots WHERE id=%s AND owner_id=%s",(pk,uid),True))

@manager.callback_query_handler(func=lambda c:c.data.startswith("m:"))
def cb_module(c):
    _,mod,pk_s=c.data.split(":"); pk=int(pk_s)
    if not owner_guard(pk,c.from_user.id): return
    if mod=="tutorials":
        return send_tutorials(c,pk)
    if mod=="startset":
        return module_start(c,pk)
    if mod=="fj":
        return module_fj(c,pk)
    if mod=="broadcast":
        return module_broadcast(c,pk)
    if mod=="autodm":
        return module_autodm(c,pk)
    if mod=="support":
        return module_support(c,pk)
    if mod=="users":
        return module_users(c,pk)
    if mod=="channels":
        return module_channels(c,pk)
    if mod=="botplans":
        return module_botplans(c,pk)
    if mod=="watermark":
        return module_watermark(c,pk)
    if mod=="stats":
        return module_stats(c,pk)
    if mod=="botsettings":
        return module_botsettings(c,pk)
    if mod=="botstatus":
        r=bot_row(pk); manager.edit_message_text(f"⏯ <b>Bot Status</b>\n\nStatus: {'🟢 Active' if r['active'] else '🔴 Paused'}",c.message.chat.id,c.message.id,reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("🔄 Toggle",callback_data=f"act:pause:{pk}")))
        return
    if mod=="deletebot":
        return delete_bot(types.SimpleNamespace(data=f"act:deletebot:{pk}",message=c.message,from_user=c.from_user))

# ---------------- TUTORIALS ----------------
def send_tutorials(c,pk=None):
    rows=db("SELECT feature_key,title FROM tutorials ORDER BY id",fetch=True)
    k=types.InlineKeyboardMarkup()
    for r in rows:k.add(types.InlineKeyboardButton(r["title"],callback_data=f"tt:{r['feature_key']}:{pk or 0}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}" if pk else "home"))
    manager.edit_message_text("📚 <b>Tutorial Center</b>\\n\\nSelect a feature:",c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("tt:"))
def cb_tutorial(c):
    _,key,pk=c.data.split(":")
    r=db("SELECT title,body FROM tutorials WHERE feature_key=%s",(key,),True)
    if not r:return
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("⬅️ Tutorials",callback_data=f"m:tutorials:{pk}" if pk!="0" else "tutorials"))
    manager.edit_message_text(f"<b>{r['title']}</b>\\n\\n{r['body']}",c.message.chat.id,c.message.id,reply_markup=k)

# ---------------- START ----------------
def module_start(c,pk):
    s=ensure_settings(pk)
    k=types.InlineKeyboardMarkup()
    for label,code in [("✏️ Edit Text","startedit"),("🖼 Set Media","startmedia"),("🔘 Buttons","buttons:start"),("👁 Preview","startpreview")]:
        k.add(types.InlineKeyboardButton(label,callback_data=f"act:{code}:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:start:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    media="None" if not s["start_media_id"] else s["start_media_type"]
    manager.edit_message_text(f"🏠 <b>Start Message</b>\n\n<b>Text:</b>\n{s['start_text']}\n\n<b>Media:</b> {media}",c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:startedit:"))
def act_start(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"start_text","bot":pk}
    manager.edit_message_text("✏️ Send new Start Message.\nVariables: {first_name}, {last_name}, {username}, {user_id}\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="start_text",content_types=["text"])
def save_start(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    if not ensure_owner(pk,m.from_user.id):return
    db("UPDATE bot_settings SET start_text=%s,updated_at=CURRENT_TIMESTAMP WHERE bot_id=%s",(m.text,pk)); audit(m.from_user.id,"update_start",pk,details="text")
    manager.send_message(m.chat.id,"✅ Start text updated.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:startmedia:"))
def start_media_prompt(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"start_media","bot":pk}
    manager.edit_message_text("🖼 Send a photo/video/document/audio to use in Start.\nSend /remove_media to clear it.\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(commands=["remove_media"])
def remove_media(m):
    st=STATE.get(m.from_user.id,{})
    if st.get("action")!="start_media":return
    pk=st["bot"]; STATE.pop(m.from_user.id,None)
    db("UPDATE bot_settings SET start_media_type=NULL,start_media_id=NULL WHERE bot_id=%s",(pk,)); manager.send_message(m.chat.id,"✅ Start media removed.",reply_markup=bot_panel(pk))

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="start_media",content_types=["photo","video","document","audio"])
def save_start_media(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    mp={"photo":("photo",m.photo[-1].file_id),"video":("video",m.video.file_id),"document":("document",m.document.file_id),"audio":("audio",m.audio.file_id)}
    typ,fid=mp[m.content_type]
    db("UPDATE bot_settings SET start_media_type=%s,start_media_id=%s WHERE bot_id=%s",(typ,fid,pk)); manager.send_message(m.chat.id,"✅ Start media saved.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:startpreview:"))
def start_preview(c):
    pk=int(c.data.split(":")[2]); s=ensure_settings(pk); u=c.from_user
    txt=variables(s["start_text"],u)
    manager.send_message(c.message.chat.id,"👁 <b>Start Preview</b>\n\n"+txt,reply_markup=make_markup(get_scope_buttons(pk,"start")))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:buttonadd:"))
def button_add(c):
    _,_,scope,pk_s=c.data.split(":"); pk=int(pk_s); STATE[c.from_user.id]={"action":"button","bot":pk,"scope":scope}
    manager.edit_message_text("🔘 Send button as:\n<code>Label | URL</code>\nor\n<code>Label | callback:data</code>\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="button",content_types=["text"])
def save_button(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]; scope=st["scope"]
    parts=[x.strip() for x in m.text.split("|",1)]
    if len(parts)!=2 or not parts[0] or not parts[1]: manager.send_message(m.chat.id,"❌ Use Label | URL"); return
    label,target=parts
    if target.startswith("http://") or target.startswith("https://"):
        db("INSERT INTO bot_buttons(bot_id,scope,label,url,row_no) VALUES(%s,%s,%s,%s,0)",(pk,scope,label,target))
    elif target.startswith("callback:"):
        db("INSERT INTO bot_buttons(bot_id,scope,label,callback_data,row_no) VALUES(%s,%s,%s,%s,0)",(pk,scope,label,target[9:]))
    else: manager.send_message(m.chat.id,"❌ URL must start with https:// or use callback:data"); return
    audit(m.from_user.id,"add_button",pk,details=scope); manager.send_message(m.chat.id,"✅ Button added.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("buttons:"))
def buttons_panel(c):
    _,scope=c.data.split(":",1); pk=int(scope.split("-")[-1]) if "-" in scope else None
    # actual callback format is buttons:start:PK
    parts=c.data.split(":"); scope=parts[1]; pk=int(parts[2])
    rows=get_scope_buttons(pk,scope)
    k=types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("➕ Add Button",callback_data=f"act:buttonadd:{scope}:{pk}"))
    for i,r in enumerate(rows): k.add(types.InlineKeyboardButton(f"🗑 {r['label'][:30]}",callback_data=f"act:buttondel:{r['label']}:{scope}:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    manager.edit_message_text(f"🔘 <b>{scope.title()} Buttons</b>\n\n"+("No buttons." if not rows else "\n".join(f"• {r['label']}" for r in rows)),c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:buttondel:"))
def button_del(c):
    parts=c.data.split(":"); label=parts[2]; scope=parts[3]; pk=int(parts[4])
    db("DELETE FROM bot_buttons WHERE bot_id=%s AND scope=%s AND label=%s",(pk,scope,label)); manager.answer_callback_query(c.id,"Button removed"); buttons_panel(types.SimpleNamespace(data=f"buttons:{scope}:{pk}",message=c.message,from_user=c.from_user))

# ---------------- FORCE JOIN ----------------
def module_fj(c,pk):
    rows=db("SELECT id,title,channel_id,link,enabled FROM bot_channels WHERE bot_id=%s ORDER BY id",(pk,),True)
    text="📢 <b>Force Join Channels</b>\\n\\n"
    if not rows:text+="No channels configured."
    else:
        text+="\\n".join(f"{'🟢' if r['enabled'] else '🔴'} {r['title']} | <code>{r['channel_id']}</code>" for r in rows)
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("➕ Add Channel",callback_data=f"act:addchannel:{pk}"))
    k.add(types.InlineKeyboardButton("🗑 Remove Channel",callback_data=f"act:delchannel:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:force_join:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    manager.edit_message_text(text,c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:addchannel:"))
def add_channel_prompt(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"add_channel","bot":pk}
    manager.edit_message_text("➕ Send:\\n<code>#g Channel Name -1001234567890 https://t.me/example</code>\\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="add_channel",content_types=["text"])
def add_channel(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    p=m.text.strip().split()
    if len(p)<4:
        manager.send_message(m.chat.id,"❌ Format invalid. Example: #g Channel_Name -1001234567890 https://t.me/example")
        return
    style,title,cid,link=p[0],p[1],p[2],p[3]
    if style not in ("#r","#g","#b","#l","#p"): style="#b"
    db("""INSERT INTO bot_channels(bot_id,channel_id,title,link,button_style)
          VALUES(%s,%s,%s,%s,%s) ON CONFLICT(bot_id,channel_id) DO UPDATE SET
          title=EXCLUDED.title,link=EXCLUDED.link,button_style=EXCLUDED.button_style,enabled=TRUE""",
       (pk,cid,title.replace("_"," "),link,style))
    audit(m.from_user.id,"add_channel",pk,details=cid)
    manager.send_message(m.chat.id,"✅ Channel added.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:delchannel:"))
def del_channel_prompt(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"del_channel","bot":pk}
    manager.edit_message_text("🗑 Send the channel ID to remove.\\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="del_channel",content_types=["text"])
def del_channel(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    db("DELETE FROM bot_channels WHERE bot_id=%s AND channel_id=%s",(pk,m.text.strip()))
    audit(m.from_user.id,"delete_channel",pk,details=m.text.strip())
    manager.send_message(m.chat.id,"✅ Channel removed.",reply_markup=bot_panel(pk))

# ---------------- BROADCAST ----------------
def module_broadcast(c,pk):
    rows=db("""SELECT COUNT(*) n,
              COUNT(*) FILTER(WHERE status='active') active
              FROM bot_users WHERE bot_id=%s""",(pk,),True)
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("✉️ New Broadcast",callback_data=f"act:newbroadcast:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:broadcast:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    manager.edit_message_text(f"📣 <b>Broadcast</b>\\n\\nUsers: <b>{rows['n']}</b>\\nActive: <b>{rows['active']}</b>",c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:newbroadcast:"))
def new_broadcast(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"broadcast","bot":pk}
    manager.edit_message_text("📣 Send the broadcast message. HTML formatting is supported.\\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="broadcast",content_types=["text","photo","video","document"])
def receive_broadcast(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    media_type=None; media_id=None; text=None
    if m.content_type=="text": text=m.text
    elif m.content_type=="photo": media_type="photo"; media_id=m.photo[-1].file_id; text=m.caption
    elif m.content_type=="video": media_type="video"; media_id=m.video.file_id; text=m.caption
    elif m.content_type=="document": media_type="document"; media_id=m.document.file_id; text=m.caption
    r=db("""INSERT INTO broadcasts(bot_id,text,media_type,media_id,created_by)
            VALUES(%s,%s,%s,%s,%s) RETURNING id""",(pk,text,media_type,media_id,m.from_user.id),True)
    audit(m.from_user.id,"create_broadcast",pk,details=str(r["id"]))
    threading.Thread(target=send_broadcast,args=(pk,r["id"]),daemon=True).start()
    manager.send_message(m.chat.id,"🚀 Broadcast queued. Delivery will continue in background.",reply_markup=bot_panel(pk))

def send_broadcast(pk,bid):
    inst=MANAGED.get(pk)
    if not inst:return
    r=db("SELECT * FROM broadcasts WHERE id=%s",(bid,),True)
    users=db("SELECT user_id FROM bot_users WHERE bot_id=%s AND status='active'",(pk,),True)
    for u in users:
        try:
            send_payload(inst,u["user_id"],r["text"],r["media_type"],r["media_id"],pk)
            db("INSERT INTO broadcast_logs(broadcast_id,user_id,status) VALUES(%s,%s,'sent') ON CONFLICT DO NOTHING",(bid,u["user_id"]))
        except Exception as e:
            db("INSERT INTO broadcast_logs(broadcast_id,user_id,status,error) VALUES(%s,%s,'failed',%s) ON CONFLICT DO NOTHING",(bid,u["user_id"],str(e)[:500]))
        time.sleep(0.04)

# ---------------- AUTO DM ----------------
def module_autodm(c,pk):
    rows=db("SELECT id,trigger_type,enabled,text,media_type,delay_seconds FROM auto_dm_rules WHERE bot_id=%s ORDER BY trigger_type",(pk,),True)
    text="💬 <b>Auto-DM</b>\n\n"+("No rules configured." if not rows else "\n".join(f"{'🟢' if r['enabled'] else '🔴'} {r['trigger_type']} • delay {r['delay_seconds']}s • {r['text'][:60]}" for r in rows))
    k=types.InlineKeyboardMarkup(); k.add(types.InlineKeyboardButton("📩 Configure Join Request",callback_data=f"act:dmconfig:join_request:{pk}")); k.add(types.InlineKeyboardButton("👤 Configure New Member",callback_data=f"act:dmconfig:new_member:{pk}"));
    for r in rows:k.add(types.InlineKeyboardButton(f"🔄 Toggle {r['trigger_type']}",callback_data=f"act:dmtoggle:{pk}:{r['id']}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:auto_dm:{pk}")); k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}")); manager.edit_message_text(text,c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:dmconfig:"))
def dm_config_prompt(c):
    _,_,trigger,pk_s=c.data.split(":");pk=int(pk_s);STATE[c.from_user.id]={"action":"dm_config","bot":pk,"trigger":trigger,"step":"text"}
    manager.edit_message_text(f"💬 <b>{trigger}</b>\n\nStep 1/2: Send DM text.\nVariables: {{first_name}} {{last_name}} {{username}} {{user_id}} {{channel_name}}\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="dm_config" and STATE.get(m.from_user.id,{}).get("step")=="text",content_types=["text"])
def dm_config_text(m):
    st=STATE[m.from_user.id];st["text"]=m.text;st["step"]="media";manager.send_message(m.chat.id,"Step 2/2: Send photo/video/document/audio, or type <code>SKIP</code>.")

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="dm_config" and STATE.get(m.from_user.id,{}).get("step")=="media",content_types=["text","photo","video","document","audio"])
def dm_config_media(m):
    st=STATE.pop(m.from_user.id);pk=st["bot"];trigger=st["trigger"];mt=mid=None
    if m.content_type=="text" and m.text.strip().upper()=="SKIP":pass
    elif m.content_type=="photo":mt="photo";mid=m.photo[-1].file_id
    elif m.content_type=="video":mt="video";mid=m.video.file_id
    elif m.content_type=="document":mt="document";mid=m.document.file_id
    elif m.content_type=="audio":mt="audio";mid=m.audio.file_id
    else:manager.send_message(m.chat.id,"❌ Send supported media or SKIP.");STATE[m.from_user.id]=st;return
    db("INSERT INTO auto_dm_rules(bot_id,trigger_type,text,media_type,media_id) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(bot_id,trigger_type) DO UPDATE SET text=EXCLUDED.text,media_type=EXCLUDED.media_type,media_id=EXCLUDED.media_id,enabled=TRUE",(pk,trigger,st["text"],mt,mid));audit(m.from_user.id,"configure_auto_dm",pk,details=trigger);manager.send_message(m.chat.id,"✅ Auto-DM configured. Use the Buttons builder later if needed.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:dmtoggle:"))
def dm_toggle(c):
    _,_,pk,rid=c.data.split(":");pk=int(pk);rid=int(rid);db("UPDATE auto_dm_rules SET enabled=NOT enabled WHERE id=%s AND bot_id=%s",(rid,pk));manager.answer_callback_query(c.id,"Updated");module_autodm(c,pk)

# ---------------- USERS / CHANNELS / PLANS ----------------
def module_users(c,pk):
    r=db("SELECT COUNT(*) total,COUNT(*) FILTER(WHERE status='active') active,COUNT(*) FILTER(WHERE status='blocked') blocked FROM bot_users WHERE bot_id=%s",(pk,),True)
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("🔎 Search User",callback_data=f"act:usersearch:{pk}"));k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:statistics:{pk}"));k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    manager.edit_message_text(f"👥 <b>Users</b>\n\nTotal: {r['total']}\n🟢 Active: {r['active']}\n🚫 Blocked: {r['blocked']}",c.message.chat.id,c.message.id,reply_markup=k)

def module_channels(c,pk):
    rows=db("SELECT id,title,channel_id,enabled,link FROM bot_channels WHERE bot_id=%s ORDER BY id",(pk,),True)
    text="📢 <b>Channels</b>\n\n"+("No channels." if not rows else "\n".join(f"{'🟢' if x['enabled'] else '🔴'} {x['title']} — <code>{x['channel_id']}</code>" for x in rows))
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("➕ Add",callback_data=f"act:addchannel:{pk}"));
    for x in rows:k.add(types.InlineKeyboardButton(f"⚙️ {x['title'][:24]}",callback_data=f"act:chanmenu:{pk}:{x['id']}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:channels:{pk}"));k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"));manager.edit_message_text(text,c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:chanmenu:"))
def channel_menu(c):
    _,_,pk,cid=c.data.split(":");pk=int(pk);cid=int(cid);r=db("SELECT * FROM bot_channels WHERE id=%s AND bot_id=%s",(cid,pk),True)
    if not r:return
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("🔄 Enable/Disable",callback_data=f"act:chantoggle:{pk}:{cid}"));k.add(types.InlineKeyboardButton("🗑 Delete",callback_data=f"act:chandel:{pk}:{cid}"));k.add(types.InlineKeyboardButton("⬅️ Channels",callback_data=f"m:channels:{pk}"));manager.edit_message_text(f"📢 <b>{r['title']}</b>\nID: <code>{r['channel_id']}</code>\nLink: {r['link']}",c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:chantoggle:"))
def channel_toggle(c):
    _,_,pk,cid=c.data.split(":");db("UPDATE bot_channels SET enabled=NOT enabled WHERE id=%s AND bot_id=%s",(int(cid),int(pk)));manager.answer_callback_query(c.id,"Channel updated");module_channels(c,int(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:chandel:"))
def channel_delete(c):
    _,_,pk,cid=c.data.split(":");db("DELETE FROM bot_channels WHERE id=%s AND bot_id=%s",(int(cid),int(pk)));manager.answer_callback_query(c.id,"Channel deleted");module_channels(c,int(pk))

def module_botplans(c,pk):
    if not owner_guard(pk,c.from_user.id): return
    expire_memberships()
    rows=db("SELECT id,name,price,duration_days,description,enabled FROM membership_plans WHERE bot_id=%s ORDER BY id DESC",(pk,),True)
    text="💎 <b>Premium Membership</b>\n\n"
    text += "\n".join(f"{'🟢' if x['enabled'] else '🔴'} <b>{x['name']}</b> • ₹{x['price']} • {x['duration_days']} days" for x in rows) if rows else "No plans created yet."
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("➕ Create Plan",callback_data=f"act:createplan:{pk}"))
    for x in rows:
        k.add(types.InlineKeyboardButton(f"⚙️ {x['name'][:28]}",callback_data=f"act:planmenu:{pk}:{x['id']}"))
    k.add(types.InlineKeyboardButton("👤 Grant / Extend / Revoke",callback_data=f"act:memberuser:{pk}"))
    k.add(types.InlineKeyboardButton("📋 Active Members",callback_data=f"act:members:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:membership:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    safe_edit(c,text,k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:createplan:"))
def create_plan_prompt(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"plan","bot":pk}
    manager.edit_message_text("💎 Send:\n<code>Name | Price | Days | Description</code>\nExample: <code>Pro | 249 | 90 | Remove watermark</code>",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="plan",content_types=["text"])
def create_plan(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]; p=[x.strip() for x in m.text.split("|",3)]
    if len(p)!=4:manager.send_message(m.chat.id,"❌ Format invalid.");return
    name,price,days,desc=p
    try:price=float(price);days=int(days)
    except:manager.send_message(m.chat.id,"❌ Price/Days invalid.");return
    if days<=0 or price<0:manager.send_message(m.chat.id,"❌ Invalid values.");return
    db("INSERT INTO membership_plans(bot_id,name,price,duration_days,description) VALUES(%s,%s,%s,%s,%s)",(pk,name,price,days,desc)); audit(m.from_user.id,"create_membership_plan",pk,details=name); manager.send_message(m.chat.id,"✅ Plan created.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:memberuser:"))
def member_user_prompt(c):
    pk=int(c.data.split(":")[2]); STATE[c.from_user.id]={"action":"member_user","bot":pk}; manager.edit_message_text("👤 Send managed user ID.",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="member_user",content_types=["text"])
def member_user(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    try:uid=int(m.text.strip())
    except:manager.send_message(m.chat.id,"❌ Invalid user ID.");return
    u=db("SELECT * FROM bot_users WHERE bot_id=%s AND user_id=%s",(pk,uid),True)
    if not u:manager.send_message(m.chat.id,"❌ User not found in this bot.",reply_markup=bot_panel(pk));return
    plans=db("SELECT id,name,duration_days,price FROM membership_plans WHERE bot_id=%s AND enabled=TRUE ORDER BY price",(pk,),True)
    k=types.InlineKeyboardMarkup()
    for p in plans:k.add(types.InlineKeyboardButton(f"💎 Grant {p['name']} ({p['duration_days']}d)",callback_data=f"act:grant:{pk}:{uid}:{p['id']}"))
    k.add(types.InlineKeyboardButton("⛔ Remove Membership",callback_data=f"act:revoke:{pk}:{uid}")); k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    manager.send_message(m.chat.id,f"👤 <b>{u['first_name'] or 'User'}</b>\nID: <code>{uid}</code>",reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:grant:"))
def grant_plan(c):
    _,_,pk,uid,pid=c.data.split(":"); pk=int(pk);uid=int(uid);pid=int(pid)
    if not ensure_owner(pk,c.from_user.id): return
    if activate_membership(pk,uid,pid,db("SELECT duration_days FROM membership_plans WHERE id=%s",(pid,),True)["duration_days"],c.from_user.id,"grant"): manager.answer_callback_query(c.id,"Membership granted ✅"); owner_notify(pk,f"💎 Membership granted to <code>{uid}</code>.")
    else: manager.answer_callback_query(c.id,"Failed",show_alert=True)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:revoke:"))
def revoke_plan(c):
    _,_,pk,uid=c.data.split(":"); remove_membership(int(pk),int(uid),c.from_user.id); manager.answer_callback_query(c.id,"Membership revoked")

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:members:"))
def active_members(c):
    pk=int(c.data.split(":")[2]); expire_memberships(); rows=db("SELECT user_id,expires_at,plan_id FROM memberships WHERE bot_id=%s AND status='active' AND expires_at>CURRENT_TIMESTAMP ORDER BY expires_at",(pk,),True)
    text="📋 <b>Active Members</b>\n\n"+("None." if not rows else "\n".join(f"• <code>{x['user_id']}</code> — until {x['expires_at'].strftime('%Y-%m-%d %H:%M UTC')}" for x in rows[:100]))
    manager.edit_message_text(text,c.message.chat.id,c.message.id,reply_markup=kb_back(pk))


# ---------------- EXTRA PRO MODULES ----------------
def safe_edit(c, text, markup=None):
    try:
        manager.edit_message_text(text, c.message.chat.id, c.message.id, reply_markup=markup)
    except Exception:
        try:
            manager.send_message(c.message.chat.id, text, reply_markup=markup)
        except Exception:
            pass

def owner_guard(pk, uid):
    return is_master(uid) or owner_of(pk, uid)

def simple_back(pk):
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("⬅️ Back", callback_data=f"bot:{pk}"))
    return k

def module_support(c, pk):
    if not owner_guard(pk,c.from_user.id): return
    r=db("""SELECT COUNT(*) total,
                    COUNT(*) FILTER(WHERE created_at > CURRENT_TIMESTAMP-INTERVAL '24 hours') today
             FROM support_map WHERE bot_id=%s""",(pk,),True)
    recent=db("""SELECT sm.user_id, bu.first_name, bu.username, MAX(al.created_at) last_at
                  FROM support_map sm LEFT JOIN bot_users bu ON bu.bot_id=sm.bot_id AND bu.user_id=sm.user_id
                  LEFT JOIN audit_logs al ON al.bot_id=sm.bot_id AND al.target_user_id=sm.user_id
                  WHERE sm.bot_id=%s GROUP BY sm.user_id,bu.first_name,bu.username
                  ORDER BY last_at DESC NULLS LAST LIMIT 8""",(pk,),True)
    lines=[]
    for x in recent:
        name=x['first_name'] or x['username'] or str(x['user_id'])
        lines.append(f"• {name} — <code>{x['user_id']}</code>")
    text=("💬 <b>Support Inbox</b>\n\n"
          f"📨 Mapped messages: <b>{r['total']}</b>\n"
          f"🕐 Last 24h: <b>{r['today']}</b>\n\n"
          + ("<b>Recent users</b>\n"+"\n".join(lines) if lines else "No support conversations yet."))
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔎 Find User",callback_data=f"act:usersearch:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:support:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    safe_edit(c,text,k)


def module_watermark(c, pk):
    if not owner_guard(pk,c.from_user.id): return
    s=ensure_settings(pk)
    active=db("SELECT COUNT(*) n FROM memberships WHERE bot_id=%s AND status='active' AND expires_at>CURRENT_TIMESTAMP",(pk,),True)['n']
    text=("💧 <b>Watermark & Branding</b>\n\n"
          f"Status: <b>{'🟢 Enabled' if s['watermark_enabled'] else '🔴 Disabled'}</b>\n"
          f"Text: <code>{s['watermark_text']}</code>\n"
          f"Active premium members: <b>{active}</b>\n\n"
          "Premium members automatically receive messages without the watermark.")
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔄 Toggle",callback_data=f"act:watermarktoggle:{pk}"))
    k.add(types.InlineKeyboardButton("✏️ Edit Text",callback_data=f"act:watermarkedit:{pk}"))
    k.add(types.InlineKeyboardButton("👁 Preview",callback_data=f"act:watermarkpreview:{pk}"))
    k.add(types.InlineKeyboardButton("💎 Manage Premium",callback_data=f"m:botplans:{pk}"))
    k.add(types.InlineKeyboardButton("📚 Tutorial",callback_data=f"tt:watermark:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"))
    safe_edit(c,text,k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:watermarktoggle:"))
def watermark_toggle(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id): return
    db("UPDATE bot_settings SET watermark_enabled=NOT watermark_enabled,updated_at=CURRENT_TIMESTAMP WHERE bot_id=%s",(pk,))
    audit(c.from_user.id,"toggle_watermark",pk)
    manager.answer_callback_query(c.id,"Watermark updated ✅")
    module_watermark(c,pk)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:watermarkedit:"))
def watermark_edit(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id): return
    STATE[c.from_user.id]={"action":"watermark_edit","bot":pk}
    manager.edit_message_text("✏️ <b>Watermark Text</b>\n\nSend the new text. Example:\n<code>⚙️ Managed via @KrishManager</code>\n\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="watermark_edit",content_types=["text"])
def watermark_save(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    if not owner_guard(pk,m.from_user.id): return
    db("UPDATE bot_settings SET watermark_text=%s,updated_at=CURRENT_TIMESTAMP WHERE bot_id=%s",(m.text.strip(),pk))
    audit(m.from_user.id,"edit_watermark",pk,details=m.text[:200])
    manager.send_message(m.chat.id,"✅ Watermark updated.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:watermarkpreview:"))
def watermark_preview(c):
    pk=int(c.data.split(":")[2]); s=ensure_settings(pk)
    txt="👋 <b>Brand Preview</b>\n\nThis is how a normal message can look."
    if s['watermark_enabled']: txt += "\n\n"+s['watermark_text']
    manager.send_message(c.message.chat.id,txt)
    manager.answer_callback_query(c.id)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:usersearch:"))
def usersearch_prompt(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id): return
    STATE[c.from_user.id]={"action":"usersearch","bot":pk}
    manager.edit_message_text("🔎 <b>Search User</b>\n\nSend Telegram user ID or @username.\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="usersearch",content_types=["text"])
def usersearch_save(m):
    st=STATE.pop(m.from_user.id); pk=st["bot"]
    q=m.text.strip().lstrip("@")
    if not owner_guard(pk,m.from_user.id): return
    if q.isdigit():
        rows=db("SELECT * FROM bot_users WHERE bot_id=%s AND user_id=%s",(pk,int(q)),True)
    else:
        rows=db("SELECT * FROM bot_users WHERE bot_id=%s AND lower(username)=lower(%s) ORDER BY last_seen DESC LIMIT 10",(pk,q),True)
    if not rows:
        manager.send_message(m.chat.id,"❌ User not found.",reply_markup=bot_panel(pk)); return
    k=types.InlineKeyboardMarkup()
    for u in rows:
        label=(u['first_name'] or u['username'] or str(u['user_id']))[:30]
        k.add(types.InlineKeyboardButton(f"👤 {label}",callback_data=f"act:user:{pk}:{u['user_id']}"))
    k.add(types.InlineKeyboardButton("⬅️ Users",callback_data=f"m:users:{pk}"))
    manager.send_message(m.chat.id,f"🔎 Found <b>{len(rows)}</b> user(s).",reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:user:"))
def user_profile(c):
    _,_,pk_s,uid_s=c.data.split(":"); pk=int(pk_s); uid=int(uid_s)
    if not owner_guard(pk,c.from_user.id): return
    u=db("SELECT * FROM bot_users WHERE bot_id=%s AND user_id=%s",(pk,uid),True)
    if not u:return
    expire_memberships()
    mem=db("""SELECT p.name,m.expires_at,m.status FROM memberships m LEFT JOIN membership_plans p ON p.id=m.plan_id
               WHERE m.bot_id=%s AND m.user_id=%s ORDER BY m.expires_at DESC LIMIT 1""",(pk,uid),True)
    notes=db("SELECT note,created_at FROM user_notes WHERE bot_id=%s AND user_id=%s ORDER BY id DESC LIMIT 5",(pk,uid),True)
    text=("👤 <b>User Profile</b>\n\n"
          f"Name: <b>{u['first_name'] or '-'} {u['last_name'] or ''}</b>\n"
          f"Username: @{u['username'] or '-'}\nID: <code>{uid}</code>\n"
          f"Status: <b>{u['status']}</b>\nJoined: {u['joined_at']}\nLast seen: {u['last_seen']}\n\n"
          f"💎 Premium: {(mem['name']+' — '+str(mem['expires_at'])) if mem and mem['status']=='active' else 'No active plan'}\n"
          f"📝 Notes: {len(notes)}")
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("💎 Manage Premium",callback_data=f"act:memberuser:{pk}"))
    k.add(types.InlineKeyboardButton("🚫 Block",callback_data=f"act:userblock:{pk}:{uid}"),types.InlineKeyboardButton("🟢 Unblock",callback_data=f"act:userunblock:{pk}:{uid}"))
    k.add(types.InlineKeyboardButton("📝 Add Note",callback_data=f"act:addnote:{pk}:{uid}"))
    k.add(types.InlineKeyboardButton("⬅️ Users",callback_data=f"m:users:{pk}"))
    safe_edit(c,text,k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:userblock:"))
def user_block(c):
    _,_,pk,uid=c.data.split(":"); pk=int(pk);uid=int(uid)
    if not owner_guard(pk,c.from_user.id):return
    db("UPDATE bot_users SET status='blocked' WHERE bot_id=%s AND user_id=%s",(pk,uid)); audit(c.from_user.id,"block_user",pk,uid)
    manager.answer_callback_query(c.id,"User blocked")
    user_profile(types.SimpleNamespace(data=f"act:user:{pk}:{uid}",message=c.message,from_user=c.from_user))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:userunblock:"))
def user_unblock(c):
    _,_,pk,uid=c.data.split(":"); pk=int(pk);uid=int(uid)
    if not owner_guard(pk,c.from_user.id):return
    db("UPDATE bot_users SET status='active' WHERE bot_id=%s AND user_id=%s",(pk,uid)); audit(c.from_user.id,"unblock_user",pk,uid)
    manager.answer_callback_query(c.id,"User unblocked")
    user_profile(types.SimpleNamespace(data=f"act:user:{pk}:{uid}",message=c.message,from_user=c.from_user))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:addnote:"))
def add_note_prompt(c):
    _,_,pk,uid=c.data.split(":"); pk=int(pk);uid=int(uid)
    if not owner_guard(pk,c.from_user.id):return
    STATE[c.from_user.id]={"action":"addnote","bot":pk,"user":uid}
    manager.edit_message_text("📝 Send the note to save for this user.\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="addnote",content_types=["text"])
def add_note_save(m):
    st=STATE.pop(m.from_user.id); pk=st['bot']; uid=st['user']
    if not owner_guard(pk,m.from_user.id):return
    db("INSERT INTO user_notes(bot_id,user_id,note,created_by) VALUES(%s,%s,%s,%s)",(pk,uid,m.text.strip(),m.from_user.id))
    audit(m.from_user.id,"add_user_note",pk,uid)
    manager.send_message(m.chat.id,"✅ Note saved.",reply_markup=bot_panel(pk))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:pause:"))
def bot_pause(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id):return
    r=bot_row(pk)
    new_state=not bool(r['active'])
    db("UPDATE managed_bots SET active=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s",(new_state,pk))
    if not new_state:
        inst=MANAGED.get(pk)
        if inst:
            try: inst.stop_polling()
            except Exception: pass
        MANAGED.pop(pk,None)
    else:
        start_managed_bot(pk)
    audit(c.from_user.id,"toggle_bot_status",pk,details=str(new_state))
    manager.answer_callback_query(c.id,"Bot status updated ✅")
    cb_bot(types.SimpleNamespace(data=f"bot:{pk}",message=c.message,from_user=c.from_user))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:deletebot:"))
def delete_bot(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id):return
    r=bot_row(pk)
    if not r:return
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("⚠️ Yes, Remove",callback_data=f"act:deleteconfirm:{pk}"))
    k.add(types.InlineKeyboardButton("⬅️ Cancel",callback_data=f"bot:{pk}"))
    safe_edit(c,f"🗑 <b>Remove @{r['bot_username'] or r['bot_name']}</b>\n\nThis removes the manager record and its configured data. The Telegram bot itself is not deleted.",k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:deleteconfirm:"))
def delete_confirm(c):
    pk=int(c.data.split(":")[2])
    if not owner_guard(pk,c.from_user.id):return
    r=bot_row(pk)
    if not r:return
    inst=MANAGED.get(pk)
    if inst:
        try:inst.stop_polling()
        except Exception:pass
    MANAGED.pop(pk,None)
    audit(c.from_user.id,"remove_managed_bot",pk,details=f"@{r['bot_username']}")
    db("DELETE FROM managed_bots WHERE id=%s",(pk,))
    manager.answer_callback_query(c.id,"Bot removed")
    cb_bots(types.SimpleNamespace(message=c.message,from_user=c.from_user))

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:plantoggle:"))
def plan_toggle(c):
    _,_,pk,pid=c.data.split(":");pk=int(pk);pid=int(pid)
    if not owner_guard(pk,c.from_user.id):return
    db("UPDATE membership_plans SET enabled=NOT enabled WHERE id=%s AND bot_id=%s",(pid,pk))
    manager.answer_callback_query(c.id,"Plan updated")
    module_botplans(c,pk)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:planmenu:"))
def plan_menu(c):
    _,_,pk,pid=c.data.split(":");pk=int(pk);pid=int(pid)
    if not owner_guard(pk,c.from_user.id):return
    r=db("SELECT * FROM membership_plans WHERE id=%s AND bot_id=%s",(pid,pk),True)
    if not r:return
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("🔄 Enable/Disable",callback_data=f"act:plantoggle:{pk}:{pid}"))
    k.add(types.InlineKeyboardButton("⬅️ Plans",callback_data=f"m:botplans:{pk}"))
    safe_edit(c,f"💎 <b>{r['name']}</b>\n\nPrice: ₹{r['price']}\nDuration: {r['duration_days']} days\nStatus: {'🟢 Enabled' if r['enabled'] else '🔴 Disabled'}\n\n{r['description'] or ''}",k)

# Better plan list: every plan is actionable.

# ---------------- STATS / SETTINGS / SUPPORT ----------------
def module_stats(c,pk):
    u=db("SELECT COUNT(*) n,COUNT(*) FILTER(WHERE status='active') active FROM bot_users WHERE bot_id=%s",(pk,),True)
    ch=db("SELECT COUNT(*) n FROM bot_channels WHERE bot_id=%s AND enabled=TRUE",(pk,),True)
    mem=db("SELECT COUNT(*) n FROM memberships WHERE bot_id=%s AND status='active' AND expires_at>CURRENT_TIMESTAMP",(pk,),True)
    b=db("SELECT COUNT(*) n FROM broadcasts WHERE bot_id=%s",(pk,),True)
    manager.edit_message_text(
      f"📊 <b>Statistics</b>\\n\\n👥 Users: {u['n']}\\n🟢 Active: {u['active']}\\n"
      f"📢 Channels: {ch['n']}\\n💎 Active Memberships: {mem['n']}\\n📣 Broadcasts: {b['n']}",
      c.message.chat.id,c.message.id,reply_markup=back_home_bot(pk))

def back_home_bot(pk):
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"));return k

def module_botsettings(c,pk):
    s=ensure_settings(pk)
    text=f"⚙️ <b>Bot Settings</b>\n\nSupport: @{s['support_username'] or ADMIN_USERNAME}\nNews: {s['news_channel'] or 'Not set'}\nLanguage: {s['language']}\nPrivacy: {'Set' if s['privacy_policy'] else 'Not set'}"
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("👤 Edit Support",callback_data=f"act:set:support:{pk}"));k.add(types.InlineKeyboardButton("📢 Edit News",callback_data=f"act:set:news:{pk}"));k.add(types.InlineKeyboardButton("🌐 Edit Language",callback_data=f"act:set:language:{pk}"));k.add(types.InlineKeyboardButton("🔒 Edit Privacy",callback_data=f"act:set:privacy:{pk}"));k.add(types.InlineKeyboardButton("⬅️ Back",callback_data=f"bot:{pk}"));manager.edit_message_text(text,c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("act:set:"))
def setting_prompt(c):
    _,_,key,pk_s=c.data.split(":");pk=int(pk_s);STATE[c.from_user.id]={"action":"setting","bot":pk,"key":key};manager.edit_message_text(f"✏️ Send new value for <b>{key}</b>.\n/cancel",c.message.chat.id,c.message.id)

@manager.message_handler(func=lambda m: STATE.get(m.from_user.id,{}).get("action")=="setting",content_types=["text"])
def setting_save(m):
    st=STATE.pop(m.from_user.id);pk=st["bot"];key=st["key"];allowed={"support":"support_username","news":"news_channel","language":"language","privacy":"privacy_policy"}
    if key not in allowed:return
    col=allowed[key];db(f"UPDATE bot_settings SET {col}=%s,updated_at=CURRENT_TIMESTAMP WHERE bot_id=%s",(m.text.strip().lstrip("@") if key=="support" else m.text,pk));audit(m.from_user.id,"update_setting",pk,details=key);manager.send_message(m.chat.id,"✅ Setting updated.",reply_markup=bot_panel(pk))

# ---------------- MEMBERSHIP FOR MANAGER USER ----------------
@manager.callback_query_handler(func=lambda c:c.data=="plans")
def user_plans(c):
    rows=db("""SELECT p.id,p.name,p.price,p.duration_days,p.description
               FROM membership_plans p JOIN managed_bots b ON b.id=p.bot_id
               WHERE b.owner_id=%s AND b.active=TRUE AND p.enabled=TRUE ORDER BY p.price""",(c.from_user.id,),True)
    k=types.InlineKeyboardMarkup()
    for r in rows:
        k.add(types.InlineKeyboardButton(f"💎 {r['name']} • ₹{r['price']} • {r['duration_days']} Days",callback_data=f"pickplan:{r['id']}"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data="home"))
    manager.edit_message_text("💧 <b>Remove Watermark / Membership</b>\\n\\nSelect a plan:",c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data.startswith("pickplan:"))
def pick_plan(c):
    pid=int(c.data.split(":")[1])
    r=db("""SELECT p.name,p.price,p.duration_days,p.description,
            COALESCE(s.support_username,%s) admin
            FROM membership_plans p JOIN managed_bots b ON b.id=p.bot_id
            LEFT JOIN bot_settings s ON s.bot_id=b.id
            WHERE p.id=%s AND p.enabled=TRUE""",(ADMIN_USERNAME,pid),True)
    if not r:return
    a=safe_username(r["admin"])
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("📩 Contact Admin",url=f"https://t.me/{a}"))
    k.add(types.InlineKeyboardButton("⬅️ Plans",callback_data="plans"))
    manager.edit_message_text(
      f"💎 <b>{r['name']}</b>\\n\\nDuration: <b>{r['duration_days']} Days</b>\\n"
      f"Price: <b>₹{r['price']}</b>\\n\\n{r['description']}\\n\\n"
      f"To activate this plan, contact <b>@{a}</b>.\\n"
      "The administrator verifies the request and activates the membership.",
      c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data=="msettings")
def manager_settings(c):
    manager.edit_message_text("⚙️ <b>Manager Settings</b>\\n\\nMaster administrator controls branding, support username, news, privacy policy, language and feature availability.",c.message.chat.id,c.message.id,reply_markup=back_home())

@manager.message_handler(commands=["admin"])
def admin_command(m):
    """Open the Master Admin panel with /admin."""
    save_manager_user(m.from_user)
    if m.from_user.id != ADMIN_ID:
        manager.reply_to(m, "⛔ <b>Access Denied</b>\n\nYou are not authorized to use the Master Admin panel.")
        return
    stats=db("""SELECT
      (SELECT COUNT(*) FROM managed_bots) bots,
      (SELECT COUNT(*) FROM manager_users) owners,
      (SELECT COUNT(*) FROM bot_users) users,
      (SELECT COUNT(*) FROM memberships WHERE status='active' AND expires_at>CURRENT_TIMESTAMP) premium""",fetch=True,one=True)
    k=types.InlineKeyboardMarkup()
    k.row(types.InlineKeyboardButton("🤖 All Managed Bots",callback_data="mbots"))
    k.row(types.InlineKeyboardButton("💎 Membership Plans",callback_data="plans"),types.InlineKeyboardButton("📚 Tutorials",callback_data="tutorials"))
    k.row(types.InlineKeyboardButton("📊 Platform Stats",callback_data="masterstats"))
    k.row(types.InlineKeyboardButton("⚙️ Manager Settings",callback_data="msettings"))
    k.row(types.InlineKeyboardButton("⬅️ Home",callback_data="home"))
    manager.send_message(m.chat.id, f"🛠 <b>Master Admin Panel</b>\n\n🤖 Bots: <b>{stats['bots']}</b>\n👥 Owners: <b>{stats['owners']}</b>\n👤 Bot Users: <b>{stats['users']}</b>\n💎 Active Premium: <b>{stats['premium']}</b>\n\nSelect a module below.", reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data=="master")
def master(c):
    if c.from_user.id!=ADMIN_ID:return
    k=types.InlineKeyboardMarkup()
    k.add(types.InlineKeyboardButton("📚 Tutorials",callback_data="tutorials"))
    k.add(types.InlineKeyboardButton("⬅️ Back",callback_data="home"))
    manager.edit_message_text("🛠 <b>Master Admin</b>\\n\\n"
                               "Use the secure PostgreSQL admin layer to manage plans, memberships, tutorials, branding and connected bots.",
                               c.message.chat.id,c.message.id,reply_markup=k)

@manager.callback_query_handler(func=lambda c:c.data=="masterstats")
def master_stats(c):
    if c.from_user.id!=ADMIN_ID:return
    x=db("""SELECT
      (SELECT COUNT(*) FROM managed_bots) bots,
      (SELECT COUNT(*) FROM manager_users) owners,
      (SELECT COUNT(*) FROM bot_users) users,
      (SELECT COUNT(*) FROM bot_channels WHERE enabled=TRUE) channels,
      (SELECT COUNT(*) FROM broadcasts) broadcasts,
      (SELECT COUNT(*) FROM memberships WHERE status='active' AND expires_at>CURRENT_TIMESTAMP) premium""",fetch=True,one=True)
    k=types.InlineKeyboardMarkup();k.add(types.InlineKeyboardButton("⬅️ Master Admin",callback_data="master"))
    safe_edit(c,f"📊 <b>Platform Statistics</b>\n\n🤖 Managed bots: <b>{x['bots']}</b>\n👥 Owners: <b>{x['owners']}</b>\n👤 Users: <b>{x['users']}</b>\n📢 Active channels: <b>{x['channels']}</b>\n📣 Broadcasts: <b>{x['broadcasts']}</b>\n💎 Active premium: <b>{x['premium']}</b>",k)

@manager.callback_query_handler(func=lambda c:c.data=="tutorials")
def root_tutorials(c): send_tutorials(c)

# ---------------- MANAGED BOT BUTTON CALLBACKS ----------------
@manager.callback_query_handler(func=lambda c:c.data.startswith("act:noop"))
def noop(c): manager.answer_callback_query(c.id)

# ---------------- MANAGED BOT RUNTIME ----------------
def decrypt_token(enc):
    return CIPHER.decrypt(enc.encode()).decode()

def send_payload(inst,user_id,text,media_type=None,media_id=None,pk=None,source_user=None,channel_name=""):
    u=source_user
    if not u:
        class U: pass
        u=U();u.id=user_id;u.first_name="";u.last_name="";u.username=""
    text=variables(text or "",u,channel_name)
    if pk and not member_active(pk,user_id):
        text=add_watermark(text,pk)
    markup=make_markup(get_scope_buttons(pk,"start") if pk else [])
    if media_type=="photo" and media_id:
        return inst.send_photo(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="video" and media_id:
        return inst.send_video(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="document" and media_id:
        return inst.send_document(user_id,media_id,caption=text or None,reply_markup=markup)
    if media_type=="audio" and media_id:
        return inst.send_audio(user_id,media_id,caption=text or None,reply_markup=markup)
    return inst.send_message(user_id,text or " ",reply_markup=markup)

def start_managed_bot(pk):
    with LOCK:
        if pk in MANAGED:return
        row=db("SELECT token_ciphertext,bot_username FROM managed_bots WHERE id=%s AND active=TRUE",(pk,),True)
        if not row:return
        try:
            inst=telebot.TeleBot(decrypt_token(row["token_ciphertext"]),parse_mode="HTML")
            me=inst.get_me()
            MANAGED[pk]=inst
            install_handlers(inst,pk)
            threading.Thread(target=lambda: managed_poll(inst,pk),daemon=True).start()
            log.info("Managed bot started pk=%s @%s",pk,me.username)
        except Exception:
            log.exception("Failed to start managed bot %s",pk)

def managed_poll(inst,pk):
    while True:
        try:
            inst.infinity_polling(skip_pending=True,timeout=30,long_polling_timeout=30)
        except Exception:
            log.exception("Managed polling crashed pk=%s",pk)
            time.sleep(5)

def render_force_join(inst,pk,user_id,u):
    channels=db("SELECT * FROM bot_channels WHERE bot_id=%s AND enabled=TRUE ORDER BY id",(pk,),True)
    missing=[]
    for ch in channels:
        try:
            member=inst.get_chat_member(ch["channel_id"],user_id)
            if member.status not in ("member","administrator","creator"):
                pending=db("SELECT 1 FROM force_join_requests WHERE bot_id=%s AND user_id=%s AND channel_id=%s",
                           (pk,user_id,ch["channel_id"]),True)
                if not pending:missing.append(ch)
        except Exception:
            # If bot cannot inspect a channel, keep it visible so owner can fix permissions.
            missing.append(ch)
    if not missing:return None
    k=types.InlineKeyboardMarkup()
    for ch in missing:
        if ch["link"]:
            k.add(types.InlineKeyboardButton(ch["button_label"],url=ch["link"]))
    k.add(types.InlineKeyboardButton("✅ Verify",callback_data=f"verify:{pk}"))
    return k

def install_handlers(inst,pk):
    @inst.message_handler(commands=["start"])
    def managed_start(m):
        save_bot_user(pk,m.from_user)
        kb=render_force_join(inst,pk,m.from_user.id,m.from_user)
        if kb:
            inst.send_message(m.chat.id,"📢 <b>Join the required channels first.</b>\\n\\nThen press Verify.",reply_markup=kb)
            return
        s=db("SELECT start_text,start_media_type,start_media_id FROM bot_settings WHERE bot_id=%s",(pk,),True)
        text=variables(s["start_text"],m.from_user)
        if member_active(pk,m.from_user.id):
            text=text.replace("⚙️ Managed via @YourManagerBot","")
        send_with_buttons(inst,m.from_user.id,text,s["start_media_type"],s["start_media_id"],get_scope_buttons(pk,"start"),pk,m.from_user)

    @inst.callback_query_handler(func=lambda c:c.data==f"verify:{pk}")
    def verify(c):
        kb=render_force_join(inst,pk,c.from_user.id,c.from_user)
        if kb:
            inst.answer_callback_query(c.id,"Some required channels are still pending.",show_alert=True)
            try:inst.edit_message_reply_markup(c.message.chat.id,c.message.id,reply_markup=kb)
            except:pass
            return
        inst.answer_callback_query(c.id,"Verified ✅")
        s=db("SELECT start_text FROM bot_settings WHERE bot_id=%s",(pk,),True)
        send_with_buttons(inst,c.from_user.id,s["start_text"],buttons=get_scope_buttons(pk,"start"),pk=pk,source_user=c.from_user)

    @inst.message_handler(content_types=["text","photo","video","document","audio","voice","animation"])
    def managed_message(m):
        if m.text and m.text.startswith("/"):return
        save_bot_user(pk,m.from_user)
        st=db("SELECT status FROM bot_users WHERE bot_id=%s AND user_id=%s",(pk,m.from_user.id),True)
        if st and st["status"]=="blocked": return
        # Basic support routing: forward a copy to owner and map the message.
        owner=db("SELECT owner_id,bot_username FROM managed_bots WHERE id=%s",(pk,),True)
        if not owner:return
        try:
            sent=manager.send_message(owner["owner_id"],
                f"💬 <b>Support — @{owner['bot_username']}</b>\n"
                f"User: <a href='tg://user?id={m.from_user.id}'>{m.from_user.first_name or 'User'}</a> "
                f"(<code>{m.from_user.id}</code>)\n\nReply to this message to answer the user.")
            db("INSERT INTO support_map(admin_message_id,bot_id,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
               (sent.message_id,pk,m.from_user.id))
            try:
                forwarded=manager.forward_message(owner["owner_id"],m.chat.id,m.message_id)
                db("INSERT INTO support_map(admin_message_id,bot_id,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                   (forwarded.message_id,pk,m.from_user.id))
            except Exception:
                pass
            inst.send_message(m.chat.id,"✉️ <b>MESSAGE SENT SUCCESSFULLY</b>")
        except Exception:
            pass

    @inst.chat_join_request_handler()
    def join_request(req):
        save_bot_user(pk,req.from_user)
        cid=str(req.chat.id)
        db("""INSERT INTO force_join_requests(bot_id,user_id,channel_id)
              VALUES(%s,%s,%s) ON CONFLICT(bot_id,user_id,channel_id)
              DO UPDATE SET requested_at=CURRENT_TIMESTAMP""",(pk,req.from_user.id,cid))
        rule=db("SELECT * FROM auto_dm_rules WHERE bot_id=%s AND trigger_type='join_request' AND enabled=TRUE",(pk,),True)
        if rule:
            def go():
                if rule["delay_seconds"]>0:time.sleep(rule["delay_seconds"])
                try:
                    send_with_buttons(inst,req.user_chat_id,rule["text"],rule["media_type"],rule["media_id"],parse_buttons(rule["buttons_json"]),pk,req.from_user,req.chat.title or "")
                except Exception:log.exception("join request dm")
            threading.Thread(target=go,daemon=True).start()
        try:
            kb=render_force_join(inst,pk,req.from_user.id,req.from_user)
            if kb:inst.send_message(req.user_chat_id,"📢 Join request received. Continue with the remaining steps.",reply_markup=kb)
        except Exception:pass
        # Deliberately do not approve or decline the request.

    # chat_member update is used for actual member events.
    @inst.chat_member_handler()
    def member_update(update):
        try:
            old=update.old_chat_member.status
            new=update.new_chat_member.status
            if new in ("member","administrator","creator") and old not in ("member","administrator","creator"):
                save_bot_user(pk,update.new_chat_member.user)
                rule=db("SELECT * FROM auto_dm_rules WHERE bot_id=%s AND trigger_type='new_member' AND enabled=TRUE",(pk,),True)
                if rule:
                    def go():
                        if rule["delay_seconds"]>0:time.sleep(rule["delay_seconds"])
                        try:
                            send_with_buttons(inst,update.new_chat_member.user.id,rule["text"],rule["media_type"],rule["media_id"],parse_buttons(rule["buttons_json"]),pk,update.new_chat_member.user,update.chat.title or "")
                        except Exception:log.exception("member dm")
                    threading.Thread(target=go,daemon=True).start()
        except Exception:log.exception("member update")

def load_all_managed():
    rows=db("SELECT id FROM managed_bots WHERE active=TRUE",fetch=True)
    for r in rows:start_managed_bot(r["id"])

# ---------------- MANAGER REPLY ROUTING ----------------
@manager.message_handler(func=lambda m: m.reply_to_message is not None, content_types=["text","photo","video","document","audio","voice","animation"])
def admin_reply(m):
    if m.from_user.id!=ADMIN_ID and not db("SELECT 1 FROM managed_bots WHERE owner_id=%s",(m.from_user.id,),True):
        return
    mp=db("SELECT bot_id,user_id FROM support_map WHERE admin_message_id=%s",(m.reply_to_message.message_id,),True)
    if not mp:return
    inst=MANAGED.get(mp["bot_id"])
    if not inst:return
    try:
        if m.content_type=="text":inst.send_message(mp["user_id"],m.text)
        elif m.content_type=="photo":inst.send_photo(mp["user_id"],m.photo[-1].file_id,caption=m.caption)
        elif m.content_type=="video":inst.send_video(mp["user_id"],m.video.file_id,caption=m.caption)
        elif m.content_type=="document":inst.send_document(mp["user_id"],m.document.file_id,caption=m.caption)
        else:inst.send_message(mp["user_id"],"📩 Admin sent a message.")
        manager.send_message(m.chat.id,"✉️ <b>MESSAGE SENT SUCCESSFULLY</b>")
    except Exception as e:
        manager.send_message(m.chat.id,f"❌ Delivery failed: {str(e)[:300]}")

# ---------------- STARTUP ----------------
def main():
    init_db()
    expire_memberships()
    load_all_managed()
    log.info("%s is online",MANAGER_NAME)
    while True:
        try:
            manager.infinity_polling(skip_pending=True,timeout=30,long_polling_timeout=30)
        except Exception:
            log.exception("Manager polling crashed")
            time.sleep(5)

if __name__=="__main__":
    main()
