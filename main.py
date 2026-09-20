import os
import io
import html
import json
import psycopg2
from psycopg2 import pool
import time
import threading
import telebot
from telebot import types

# ============================================================
# CONFIG
# ============================================================
API_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not API_TOKEN or ":" not in API_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing or invalid. Add the complete BotFather token in Railway Variables.")
if ADMIN_ID == 0:
    raise RuntimeError("ADMIN_ID is missing. Add your numeric Telegram user ID in Railway Variables.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set. Add a Railway PostgreSQL service and connect its DATABASE_URL variable.")

bot = telebot.TeleBot(API_TOKEN, parse_mode="HTML")

# ============================================================
# DATABASE
# ============================================================
DB_LOCK = threading.Lock()

def db_connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=30)

def db_execute(query, params=(), fetchone=False, fetchall=False, commit=False):
    """Small PostgreSQL helper kept separate so existing bot features stay unchanged."""
    with DB_LOCK:
        conn = db_connect()
        try:
            cur = conn.cursor()
            cur.execute(query, params)
            result = cur.fetchone() if fetchone else (cur.fetchall() if fetchall else None)
            if commit:
                conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def init_db():
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                status TEXT DEFAULT 'active',
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT ''
            )
        """)
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT DEFAULT ''")
        c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''")

        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                val TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id SERIAL PRIMARY KEY,
                channel_id TEXT UNIQUE,
                title TEXT,
                link TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS live_map (
                admin_message_id INTEGER PRIMARY KEY,
                user_id BIGINT
            )
        """)

        # Users who have submitted a Telegram join request to a configured
        # private channel are treated as having completed that channel's
        # Force-Join step. The bot does NOT approve the request.
        # Every controller user gets an owner row before a managed bot is
        # inserted.  Older deployments already have manager_users, so all
        # statements below are deliberately additive/backward-compatible.
        c.execute("""
            CREATE TABLE IF NOT EXISTS manager_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''")
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS first_name TEXT DEFAULT ''")
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS last_name TEXT DEFAULT ''")
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")
        c.execute("ALTER TABLE manager_users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_bots (
                id SERIAL PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                bot_token TEXT UNIQUE NOT NULL,
                bot_id BIGINT UNIQUE NOT NULL,
                bot_username TEXT DEFAULT '',
                bot_name TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Backward-compatible migration: existing Railway tables are not changed by CREATE TABLE IF NOT EXISTS.
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS owner_id BIGINT")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS owner_user_id BIGINT")
        # Existing deployments may have owner_id NOT NULL while newer code used owner_user_id.
        # Keep both columns compatible and use owner_id as the canonical owner field.
        c.execute("UPDATE managed_bots SET owner_id=owner_user_id WHERE owner_id IS NULL AND owner_user_id IS NOT NULL")
        c.execute("UPDATE managed_bots SET owner_user_id=owner_id WHERE owner_user_id IS NULL AND owner_id IS NOT NULL")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS bot_token TEXT")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS bot_id BIGINT")
        # Older deployments may have a required token_ciphertext column.  The
        # controller now keeps bot_token as the runtime token, while retaining
        # the legacy column only for schema compatibility.
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS token_ciphertext TEXT")
        c.execute("ALTER TABLE managed_bots ALTER COLUMN token_ciphertext DROP NOT NULL")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS bot_username TEXT DEFAULT ''")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS bot_name TEXT DEFAULT ''")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'")
        c.execute("ALTER TABLE managed_bots ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP")

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_settings (
                bot_id INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
                key TEXT NOT NULL,
                val TEXT,
                PRIMARY KEY (bot_id, key)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_channels (
                id SERIAL PRIMARY KEY,
                bot_id INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
                channel_id TEXT NOT NULL,
                title TEXT,
                link TEXT,
                UNIQUE(bot_id, channel_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_users (
                bot_id INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                joined_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, user_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_join_requests (
                bot_id INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                channel_id TEXT NOT NULL,
                requested_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (bot_id, user_id, channel_id)
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS managed_live_map (
                bot_id INTEGER NOT NULL REFERENCES managed_bots(id) ON DELETE CASCADE,
                admin_message_id INTEGER NOT NULL,
                user_id BIGINT NOT NULL,
                PRIMARY KEY (bot_id, admin_message_id)
            )
        """)

        defaults = [
            ("fj_text", "नमस्ते <b>{first_name}</b>\n\nसभी चैनल जोड़ें!"),
            ("fj_media_id", "NONE"),
            ("fj_media_type", "NONE"),
            ("fj_verify_btn", "#g जाँच करें / Try Again"),
            ("start_text", "👋 Welcome <b>{first_name}</b>!\n\nHere you control your bot via premium features.\n\n🤖 Now connect your bot to get started."),
            ("start_media_id", "NONE"),
            ("start_media_type", "NONE"),
            ("start_buttons", "#p सहायता | https://t.me/\n#g मुख्य चैनल | https://t.me/"),
            ("start_pin", "NO"),

            ("bcast_text", "📢 <b>नई सूचना!</b>"),
            ("bcast_media_id", "NONE"),
            ("bcast_media_type", "NONE"),
            ("bcast_buttons", "#l विशेष ऑफर | https://t.me/"),
            ("bcast_pin", "NO")
        ]

        for k, v in defaults:
            c.execute(
                "INSERT INTO settings (key, val) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING",
                (k, v)
            )

        conn.commit()
        conn.close()

init_db()

def get_set(key):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT val FROM settings WHERE key=%s", (key,))
        row = c.fetchone()
        conn.close()
    return row[0] if row else "NONE"

def set_val(key, val):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO settings (key, val) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET val=EXCLUDED.val",
            (key, str(val))
        )
        conn.commit()
        conn.close()

def add_user(user_id, user=None):
    first_name=(getattr(user,'first_name','') or '') if user else ''
    username=(getattr(user,'username','') or '') if user else ''
    with DB_LOCK:
        conn=db_connect(); c=conn.cursor()
        c.execute("""INSERT INTO users (user_id,status,first_name,username)
                     VALUES (%s,'active',%s,%s)
                     ON CONFLICT (user_id) DO UPDATE SET
                       status='active', first_name=CASE WHEN EXCLUDED.first_name<>'' THEN EXCLUDED.first_name ELSE users.first_name END,
                       username=CASE WHEN EXCLUDED.username<>'' THEN EXCLUDED.username ELSE users.username END""",
                  (user_id,first_name,username))
        conn.commit(); conn.close()

def get_main_user(user_id):
    with DB_LOCK:
        conn=db_connect(); c=conn.cursor()
        try:
            c.execute("SELECT user_id,first_name,username FROM users WHERE user_id=%s",(user_id,))
            row=c.fetchone()
        except Exception:
            row=None
        conn.close()
    if not row: return None
    class U: pass
    u=U(); u.id=row[0]; u.first_name=row[1] or 'User'; u.username=row[2] or ''
    return u

def get_channels():
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT id, channel_id, title, link FROM channels ORDER BY id")
        rows = c.fetchall()
        conn.close()
    return rows

# ============================================================
# HELPERS
# ============================================================
def get_button_style_and_text(text):
    """
    Parse native Telegram button styles.

    #r = red (danger)
    #g = green (success)
    #b = blue (primary)

    #l and #p are kept for compatibility and map to blue,
    because Telegram currently exposes only red, green and blue
    button backgrounds through the Bot API.
    """
    if not text:
        return "", None

    raw = text.strip()
    style_map = {
        "#r": "danger",
        "#g": "success",
        "#b": "primary",
        "#l": "primary",
        "#p": "primary",
    }

    lower = raw.lower()
    for code, style in style_map.items():
        if lower.startswith(code):
            return raw[len(code):].strip(), style

    return raw, None

def parse_colors(text):
    """Backward-compatible helper: remove a color prefix from text."""
    clean_text, _ = get_button_style_and_text(text)
    return clean_text

def make_url_button(title, url):
    """Create a native Telegram styled inline URL button."""
    clean_title, style = get_button_style_and_text(title)
    kwargs = {
        "text": clean_title,
        "url": url.strip(),
    }
    if style:
        kwargs["style"] = style
    return types.InlineKeyboardButton(**kwargs)

def make_callback_button(title, callback_data, default_style=None):
    """Create a callback button; a #r/#g/#b prefix controls its color."""
    clean_title, style = get_button_style_and_text(title)
    kwargs = {
        "text": clean_title,
        "callback_data": callback_data,
    }
    if style or default_style:
        kwargs["style"] = style or default_style
    return types.InlineKeyboardButton(**kwargs)

def build_keyboard_from_string(btn_str):
    """
    Build configurable URL buttons from one button per line:

        #g 🟢 GREEN BUTTON | https://example.com
        #b 🔵 BLUE BUTTON  | https://example.com
        #r 🔴 RED BUTTON   | https://example.com

    The #r/#g/#b prefix changes the actual Telegram button background;
    it is not shown in the final button text.
    """
    markup = types.InlineKeyboardMarkup(row_width=2)

    if not btn_str or btn_str == "NONE":
        return markup

    for line in btn_str.strip().splitlines():
        if "|" not in line:
            continue

        title, url = line.split("|", 1)
        title = title.strip()
        url = url.strip()

        if title and url:
            markup.add(make_url_button(title, url))

    return markup

def is_admin(user_id):
    return user_id == ADMIN_ID

def get_unjoined_channels(user_id):
    channels = get_channels()
    unjoined = []

    for ch in channels:
        channel_id = str(ch[1])

        # A pending Join Request is enough for this bot's Force-Join flow.
        # Telegram may still report the user as a non-member until an admin
        # approves the request, so check our recorded request before treating
        # the channel as unjoined. The bot never approves the request.
        try:
            requested = db_execute(
                "SELECT 1 FROM join_requests WHERE user_id=%s AND channel_id=%s",
                (user_id, channel_id),
                fetchone=True
            )
            if requested:
                continue
        except Exception:
            # If the request table cannot be checked, fall back to the real
            # Telegram membership check below.
            pass

        try:
            member = bot.get_chat_member(chat_id=ch[1], user_id=user_id)

            # These statuses count as joined.
            joined_statuses = ("member", "administrator", "creator")

            if member.status not in joined_statuses:
                unjoined.append(ch)

        except Exception:
            # If bot cannot check the channel, keep it in the
            # force-join list instead of incorrectly approving.
            unjoined.append(ch)

    return unjoined


# ============================================================
# TELEGRAM JOIN REQUEST DETECTION
# ============================================================
@bot.chat_join_request_handler()
def handle_join_request(join_request):
    """
    Detect a user's Join Request for a configured private channel.

    IMPORTANT: This handler intentionally DOES NOT approve the request.
    It only records the request so the Force-Join flow can treat that
    channel as satisfied and lets the user continue in the bot.
    """
    user = join_request.from_user
    channel_id = str(join_request.chat.id)

    # Only apply this special flow to channels configured in Force Join.
    configured = None
    for ch in get_channels():
        if str(ch[1]) == channel_id:
            configured = ch
            break

    if configured is None:
        return

    try:
        db_execute(
            """
            INSERT INTO join_requests (user_id, channel_id)
            VALUES (%s, %s)
            ON CONFLICT (user_id, channel_id)
            DO UPDATE SET requested_at=CURRENT_TIMESTAMP
            """,
            (user.id, channel_id),
            commit=True
        )

        # Never call approve_chat_join_request() here.
        # The pending request remains pending until the channel admin acts.
        add_user(user.id, user)

        # If all configured Force-Join channels are now either joined or
        # requested, continue straight to the normal Start page. Otherwise
        # show the remaining Force-Join buttons.
        unjoined = get_unjoined_channels(user.id)
        if not unjoined:
            send_start_page(user.id, user)
        else:
            send_force_join_page(user.id, user)

    except Exception as e:
        print("Join request handling error:", e)

# ============================================================
# USER PAGES
# ============================================================
def send_force_join_page(chat_id, user):
    unjoined = get_unjoined_channels(user.id)

    txt = _render_template(get_set("fj_text_html") or get_set("fj_text"), user)

    m_id = get_set("fj_media_id")
    m_type = get_set("fj_media_type")

    verify_txt = get_set("fj_verify_btn")

    markup = types.InlineKeyboardMarkup(row_width=2)

    btns = []
    for ch in unjoined:
        title = ch[2]
        link = ch[3]

        if title and link:
            btns.append(
                make_url_button(title, link)
            )

    if btns:
        markup.add(*btns)

    verify_title, verify_style = get_button_style_and_text(verify_txt)
    verify_kwargs = {
        "text": verify_title,
        "callback_data": "verify_force_join",
        "style": verify_style or "primary",
    }
    markup.add(types.InlineKeyboardButton(**verify_kwargs))

    try:
        txt=_render_template(get_set("fj_text_html") or get_set("fj_text"), user)
        if m_type == "photo" and m_id != "NONE":
            bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        elif m_type == "video" and m_id != "NONE":
            bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        else:
            bot.send_message(
                chat_id,
                txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

    except Exception:
        bot.send_message(
            chat_id,
            txt,
            reply_markup=markup,
            parse_mode="HTML"
        )

def send_start_page(chat_id, user):
    txt = _render_template(get_set("start_text_html") or get_set("start_text"), user)

    m_id = get_set("start_media_id")
    m_type = get_set("start_media_type")

    markup = build_keyboard_from_string(
        get_set("start_buttons")
    )

    try:
        if m_type == "photo" and m_id != "NONE":
            msg = bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        elif m_type == "video" and m_id != "NONE":
            msg = bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        else:
            msg = bot.send_message(
                chat_id,
                txt,
                reply_markup=markup
            )

        if get_set("start_pin") == "YES":
            try:
                bot.pin_chat_message(
                    chat_id,
                    msg.message_id
                )
            except Exception:
                pass

    except Exception as e:
        bot.send_message(
            chat_id,
            "❌ Start page error: " + str(e)
        )

# ============================================================
# START / ADMIN COMMANDS
# ============================================================
@bot.message_handler(commands=["start"])
def start_cmd(message):
    user_id = message.from_user.id
    add_user(user_id)

    # STRICT FLOW:
    # 1) Check Force Join FIRST.
    # 2) If even one configured channel is not joined, NEVER show welcome.
    # 3) Welcome/Start page is shown only after successful verification.
    unjoined = get_unjoined_channels(user_id)

    if len(unjoined) > 0:
        send_force_join_page(
            message.chat.id,
            message.from_user
        )
        return

    # Controller welcome. Every user can use the controller; only the
    # controller admin gets the global admin panel.
    send_start_page(message.chat.id, message.from_user)

    mk = types.InlineKeyboardMarkup(row_width=1)
    mk.add(types.InlineKeyboardButton("➕ Connect Bot", callback_data="connect_bot"))
    mk.add(types.InlineKeyboardButton("🤖 My Connected Bots", callback_data="my_bots"))
    if is_admin(user_id):
        mk.add(types.InlineKeyboardButton("👑 Controller Admin", callback_data="controller_admin"))
    bot.send_message(
        message.chat.id,
        "🤖 <b>Multi Control Bot</b>\n\nConnect your own Telegram bot and manage it from here.",
        reply_markup=mk
    )

@bot.message_handler(commands=["mybots", "connectbot"])
def mybots_cmd(message):
    show_managed_bots(message.chat.id)

@bot.message_handler(commands=["admin"])
def admin_cmd(message):
    if is_admin(message.from_user.id):
        show_admin_main(message.chat.id)

def show_admin_main(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "⚙️ स्टार्ट",
            callback_data="menu_start"
        ),
        types.InlineKeyboardButton(
            "🔐 फ़ोर्स ज्वाइन",
            callback_data="menu_fj"
        ),
        types.InlineKeyboardButton(
            "📢 प्रसारण",
            callback_data="menu_bc"
        ),
        types.InlineKeyboardButton(
            "📺 चैनल",
            callback_data="menu_channels"
        ),
        types.InlineKeyboardButton(
            "📊 आंकड़े",
            callback_data="menu_stats"
        ),
        types.InlineKeyboardButton(
            "🤖 My Bots",
            callback_data="my_bots"
        ),
        types.InlineKeyboardButton(
            "👑 Controller Users",
            callback_data="controller_users"
        ),
        types.InlineKeyboardButton(
            "🤖 All Connected Bots",
            callback_data="controller_bots"
        ),
        types.InlineKeyboardButton(
            "📈 Controller Stats",
            callback_data="controller_stats"
        )
    )

    bot.send_message(
        chat_id,
        "👑 <b>एडमिन कंट्रोल पैनल</b>",
        reply_markup=markup
    )

# ============================================================
# ADMIN KEYBOARDS
# ============================================================
def get_modular_keyboard(prefix, is_bcast=False):
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "🖼️ Media",
            callback_data=prefix + "_edit_media"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_media"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "abc Text",
            callback_data=prefix + "_edit_text"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_text"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🔘 Buttons",
            callback_data=prefix + "_edit_btn"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data=prefix + "_see_btn"
        )
    )

    pin_key = "bcast_pin" if is_bcast else "start_pin"
    pin_status = (
        "✅ YES"
        if get_set(pin_key) == "YES"
        else "❌ NO"
    )

    markup.add(
        types.InlineKeyboardButton(
            "📌 Pin",
            callback_data=prefix + "_toggle_pin"
        ),
        types.InlineKeyboardButton(
            pin_status,
            callback_data=prefix + "_toggle_pin"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "👀 Full preview",
            callback_data=prefix + "_full_prev"
        )
    )

    if is_bcast:
        markup.add(
            types.InlineKeyboardButton(
                "⬅️ Back",
                callback_data="adm_home"
            ),
            types.InlineKeyboardButton(
                "Next ➡️",
                callback_data="bc_next_send"
            )
        )
    else:
        markup.add(
            types.InlineKeyboardButton(
                "🏠 Menu",
                callback_data="adm_home"
            ),
            types.InlineKeyboardButton(
                "⬅️ Back",
                callback_data="adm_home"
            )
        )

    return markup

def get_fj_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)

    markup.add(
        types.InlineKeyboardButton(
            "🖼️ Media",
            callback_data="fj_edit_media"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_media"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "📝 Text",
            callback_data="fj_edit_text"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_text"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🔘 Verify Button",
            callback_data="fj_edit_btn"
        ),
        types.InlineKeyboardButton(
            "👀 See",
            callback_data="fj_see_btn"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "👀 Full preview",
            callback_data="fj_full_prev"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🏠 Menu",
            callback_data="adm_home"
        ),
        types.InlineKeyboardButton(
            "⬅️ Back",
            callback_data="adm_home"
        )
    )

    return markup



# ============================================================
# TELEGRAM FORMAT / TEMPLATE HELPERS
# ============================================================
def _message_html(message, caption=False):
    """Return Telegram's rendered HTML while preserving entities/quotes."""
    if caption:
        value=getattr(message,'html_caption',None)
        if value:
            return value
        value=getattr(message,'caption',None)
    else:
        value=getattr(message,'html_text',None)
        if value:
            return value
        value=getattr(message,'text',None)
    return value or ''

def _safe_placeholder(value):
    return html.escape(str(value or 'User'), quote=False)

def _render_template(value, user=None):
    """Replace supported placeholders without breaking Telegram HTML formatting."""
    if value is None:
        return ''
    if user is None:
        class _U: pass
        user=_U(); user.first_name='User'; user.username=''; user.id=''
    first=getattr(user,'first_name','') or 'User'
    username=getattr(user,'username','') or ''
    uid=getattr(user,'id','') or ''
    replacements={
        '{first_name}': _safe_placeholder(first),
        '{username}': _safe_placeholder('@'+username if username else ''),
        '{user_id}': html.escape(str(uid),quote=False),
    }
    out=str(value)
    for k,v in replacements.items(): out=out.replace(k,v)
    return out

def _store_formatted_setting(setter, text_key, message):
    """Store raw fallback plus Telegram-generated HTML for exact formatting."""
    raw=getattr(message,'text',None)
    if raw is None: raw=getattr(message,'caption',None)
    setter(text_key, raw or '')
    rendered=_message_html(message, caption=(getattr(message,'caption',None) is not None and not getattr(message,'text',None)))
    if rendered:
        setter(text_key+'_html', rendered)

def _get_formatted_setting(getter, text_key):
    rendered=getter(text_key+'_html')
    if rendered and rendered != 'NONE': return rendered
    return getter(text_key)

# ============================================================
# CONNECTED-BOT CONTROLLER
# ============================================================
MANAGED_BOTS = {}
MANAGED_STOPS = {}
MANAGED_LOCK = threading.Lock()
MANAGED_FLOW = {}

MANAGED_DEFAULTS = {
    "fj_text": "नमस्ते <b>{first_name}</b>\n\nकृपया सभी चैनल join करें।",
    "fj_media_id": "NONE", "fj_media_type": "NONE",
    "fj_verify_btn": "#g जाँच करें / Try Again",
    "start_text": "👋 स्वागत है <b>{first_name}</b>!",
    "start_media_id": "NONE", "start_media_type": "NONE",
    "start_buttons": "NONE", "start_pin": "NO",
    "bcast_text": "📢 <b>नई सूचना!</b>",
    "bcast_media_id": "NONE", "bcast_media_type": "NONE",
    "bcast_buttons": "NONE", "bcast_pin": "NO",
    "autodm_text": "👋 Welcome! धन्यवाद join करने के लिए।",
    "autodm_media_id": "NONE", "autodm_media_type": "NONE",
    "autodm_buttons": "NONE", "autodm_pin": "NO",
    "autodm_enabled": "YES",
    "dmleave_text": "👋 <b>{first_name}</b>, आप हमारे चैनल से leave हो गए हैं।",
    "dmleave_media_id": "NONE", "dmleave_media_type": "NONE",
    "dmleave_buttons": "NONE", "dmleave_pin": "NO",
    "dmleave_enabled": "YES",
}

def mdb(q, params=(), one=False, all_rows=False, commit=False):
    return db_execute(q, params, fetchone=one, fetchall=all_rows, commit=commit)

def mget(bot_db_id, key):
    row=mdb("SELECT val FROM managed_settings WHERE bot_id=%s AND key=%s",(bot_db_id,key),one=True)
    return row[0] if row else MANAGED_DEFAULTS.get(key,"NONE")

def mset(bot_db_id,key,val):
    mdb("""INSERT INTO managed_settings(bot_id,key,val) VALUES(%s,%s,%s)
           ON CONFLICT(bot_id,key) DO UPDATE SET val=EXCLUDED.val""",(bot_db_id,key,str(val)),commit=True)

def ensure_managed_defaults(bot_db_id):
    for k,v in MANAGED_DEFAULTS.items():
        mdb("INSERT INTO managed_settings(bot_id,key,val) VALUES(%s,%s,%s) ON CONFLICT(bot_id,key) DO NOTHING",(bot_db_id,k,v),commit=True)

def managed_info(bot_db_id):
    return mdb("SELECT id,owner_id,bot_token,bot_id,bot_username,bot_name,status FROM managed_bots WHERE id=%s",(bot_db_id,),one=True)

def managed_info_any(bot_db_id):
    return mdb("SELECT id,owner_id,bot_token,bot_id,bot_username,bot_name,status FROM managed_bots WHERE id=%s",
               (bot_db_id,),one=True)

def managed_is_owner_any_status(bot_db_id,user_id):
    row=managed_info_any(bot_db_id)
    return bool(row and int(row[1] or 0)==int(user_id))

def reconnect_existing_bot(message, bot_db_id):
    owner_id=message.from_user.id
    token=(message.text or '').strip()
    if ':' not in token:
        bot.send_message(message.chat.id,'❌ Invalid BotFather token.')
        return
    if not managed_is_owner_any_status(bot_db_id,owner_id):
        bot.send_message(message.chat.id,'❌ यह आपका bot नहीं है.')
        return
    try:
        ensure_manager_user(message.from_user)
        mb=telebot.TeleBot(token,parse_mode='HTML')
        me=mb.get_me()
        row=managed_info_any(bot_db_id)
        if not row or int(row[1] or 0)!=owner_id:
            raise ValueError('Bot ownership check failed.')
        if int(me.id) != int(row[3]):
            bot.send_message(message.chat.id,
                '❌ यह उसी bot का token नहीं है.\n'
                'उसी bot का current BotFather token भेजें.')
            return

        old=MANAGED_BOTS.pop(bot_db_id,None)
        MANAGED_STOPS.pop(bot_db_id,None)
        try:
            if old: old.stop_polling()
        except Exception:
            pass

        mdb("""UPDATE managed_bots
               SET bot_token=%s, token_ciphertext=%s,
                   bot_username=%s, bot_name=%s, status='active'
               WHERE id=%s AND owner_id=%s""",
            (token,token,me.username or '',me.first_name or '',bot_db_id,owner_id),
            commit=True)
        ensure_managed_defaults(bot_db_id)
        threading.Thread(target=run_managed_bot,args=(bot_db_id,token),daemon=True).start()

        bot.send_message(
            message.chat.id,
            f'✅ <b>@{me.username or me.first_name}</b> reconnected successfully! 🎉\n\n'
            '♻️ Your previous settings and data are still saved.',
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton('⚙️ Open Control Panel',callback_data=f'mbot_{bot_db_id}')
            )
        )
    except Exception as e:
        bot.send_message(message.chat.id,'❌ Reconnect failed: '+str(e)[:500])

def owner_bot_ids(owner_id):
    return mdb("SELECT id,bot_username,bot_name,status FROM managed_bots WHERE owner_id=%s ORDER BY id",(owner_id,),all_rows=True) or []

def managed_channels(bot_db_id):
    return mdb("SELECT id,channel_id,title,link FROM managed_channels WHERE bot_id=%s ORDER BY id",(bot_db_id,),all_rows=True) or []

def managed_is_owner(bot_db_id,user_id):
    row=mdb("SELECT 1 FROM managed_bots WHERE id=%s AND owner_id=%s AND status='active'",(bot_db_id,user_id),one=True)
    return bool(row)

def managed_add_user(bot_db_id,user):
    try:
        mdb("""INSERT INTO managed_users(bot_id,user_id,first_name,username,status,last_seen_at)
             VALUES(%s,%s,%s,%s,'active',CURRENT_TIMESTAMP)
             ON CONFLICT(bot_id,user_id) DO UPDATE SET first_name=EXCLUDED.first_name,username=EXCLUDED.username,last_seen_at=CURRENT_TIMESTAMP""",
            (bot_db_id,user.id,user.first_name or '',user.username or ''),commit=True)
    except Exception as e: print('managed user save:',e)

def managed_blocked(bot_db_id,user_id):
    row=mdb("SELECT status FROM managed_users WHERE bot_id=%s AND user_id=%s",(bot_db_id,user_id),one=True)
    return bool(row and row[0]=='blocked')

def managed_button_markup(bot_db_id,key):
    return build_keyboard_from_string(mget(bot_db_id,key))

def managed_user_for_chat(bot_db_id, chat_id):
    row=mdb("SELECT user_id,first_name,username FROM managed_users WHERE bot_id=%s AND user_id=%s",(bot_db_id,chat_id),one=True)
    if not row: return None
    class U: pass
    u=U(); u.id=row[0]; u.first_name=row[1] or 'User'; u.username=row[2] or ''
    return u

def managed_send_payload(mbot, chat_id, bot_db_id, prefix, pin=False, user=None):
    if user is None:
        user=managed_user_for_chat(bot_db_id,chat_id)
    txt=_render_template(_get_formatted_setting(mget,prefix+"_text"),user)
    mid=mget(bot_db_id,prefix+"_media_id")
    mt=mget(bot_db_id,prefix+"_media_type")
    markup=managed_button_markup(bot_db_id,prefix+"_buttons")
    msg=None
    try:
        if mt=='photo' and mid!='NONE':
            msg=mbot.send_photo(chat_id,mid,caption=txt,reply_markup=markup,parse_mode='HTML')
        elif mt=='video' and mid!='NONE':
            msg=mbot.send_video(chat_id,mid,caption=txt,reply_markup=markup,parse_mode='HTML')
        else:
            msg=mbot.send_message(chat_id,txt,reply_markup=markup,parse_mode='HTML')
    except Exception:
        # Old settings may contain plain/raw HTML. Keep the legacy fallback.
        if mt=='photo' and mid!='NONE': msg=mbot.send_photo(chat_id,mid,caption=txt,reply_markup=markup)
        elif mt=='video' and mid!='NONE': msg=mbot.send_video(chat_id,mid,caption=txt,reply_markup=markup)
        else: msg=mbot.send_message(chat_id,txt,reply_markup=markup)
    if pin and msg:
        try: mbot.pin_chat_message(chat_id,msg.message_id)
        except Exception: pass
    return msg

def managed_unjoined(bot_db_id,user_id,mbot):
    out=[]
    for ch in managed_channels(bot_db_id):
        cid=str(ch[1])
        requested=mdb("SELECT 1 FROM managed_join_requests WHERE bot_id=%s AND user_id=%s AND channel_id=%s",(bot_db_id,user_id,cid),one=True)
        if requested: continue
        try:
            cm=mbot.get_chat_member(cid,user_id)
            if cm.status in ('member','administrator','creator') or (cm.status=='restricted' and getattr(cm,'is_member',False)):
                continue
        except Exception:
            # If Telegram cannot verify a channel, keep it in the list so the owner can fix it.
            pass
        out.append(ch)
    return out

def managed_start_page(mbot,bot_db_id,user):
    if managed_blocked(bot_db_id,user.id): return
    missing=managed_unjoined(bot_db_id,user.id,mbot)
    if missing:
        mk=types.InlineKeyboardMarkup(row_width=2)
        for ch in missing:
            if ch[2] and ch[3]: mk.add(make_url_button(ch[2],ch[3]))
        vt,vs=get_button_style_and_text(mget(bot_db_id,'fj_verify_btn'))
        mk.add(types.InlineKeyboardButton(text=vt or 'Verify',callback_data=f'mverify_{bot_db_id}',style=vs or 'primary'))
        txt=_render_template(_get_formatted_setting(mget,'fj_text'),user)
        mid=mget(bot_db_id,'fj_media_id'); mt=mget(bot_db_id,'fj_media_type')
        try:
            if mt=='photo' and mid!='NONE': mbot.send_photo(user.id,mid,caption=txt,reply_markup=mk,parse_mode='HTML')
            elif mt=='video' and mid!='NONE': mbot.send_video(user.id,mid,caption=txt,reply_markup=mk,parse_mode='HTML')
            else: mbot.send_message(user.id,txt,reply_markup=mk,parse_mode='HTML')
        except Exception:
            if mt=='photo' and mid!='NONE': mbot.send_photo(user.id,mid,caption=txt,reply_markup=mk)
            elif mt=='video' and mid!='NONE': mbot.send_video(user.id,mid,caption=txt,reply_markup=mk)
            else: mbot.send_message(user.id,txt,reply_markup=mk)
        return
    managed_send_payload(mbot,user.id,bot_db_id,'start',mget(bot_db_id,'start_pin')=='YES',user)

def managed_autodm(mbot,bot_db_id,user_id,user=None):
    if mget(bot_db_id,'autodm_enabled')!='YES' or managed_blocked(bot_db_id,user_id): return
    try:
        managed_send_payload(mbot,user_id,bot_db_id,'autodm',mget(bot_db_id,'autodm_pin')=='YES',user)
    except Exception as e: print('AutoDM error:',e)

def managed_dm_on_leave(mbot,bot_db_id,user_id,user):
    """Send configured DMOnLeave payload after a channel/supergroup leave event.

    Telegram only allows the private DM if the user is reachable by the bot
    (for example, the user has started the bot before). A channel leave event
    itself does not provide a special private-chat permission.
    """
    if mget(bot_db_id,'dmleave_enabled') != 'YES':
        print(f'DMOnLeave disabled for bot {bot_db_id}')
        return False
    if managed_blocked(bot_db_id,user_id):
        print(f'DMOnLeave blocked user {user_id} for bot {bot_db_id}')
        return False
    try:
        managed_add_user(bot_db_id,user)
        msg=managed_send_payload(
            mbot,user_id,bot_db_id,'dmleave',
            mget(bot_db_id,'dmleave_pin')=='YES',user
        )
        try:
            mdb("UPDATE managed_users SET status='left',last_seen_at=CURRENT_TIMESTAMP WHERE bot_id=%s AND user_id=%s",
                (bot_db_id,user_id),commit=True)
        except Exception:
            pass
        print(f'DMOnLeave sent: bot={bot_db_id} user={user_id} message={getattr(msg,"message_id",None)}')
        return bool(msg)
    except Exception as e:
        print(f'DMOnLeave send failed: bot={bot_db_id} user={user_id} error={e}')
        return False

def _safe_delete(mbot,chat_id,message_id):
    try: mbot.delete_message(chat_id,message_id)
    except Exception: pass

def managed_register_handlers(mbot,bot_db_id):
    @mbot.message_handler(commands=['start'])
    def _mstart(message):
        if message.chat.type!='private': return
        managed_add_user(bot_db_id,message.from_user)
        managed_start_page(mbot,bot_db_id,message.from_user)

    @mbot.callback_query_handler(func=lambda call: call.data==f'mverify_{bot_db_id}')
    def _verify(call):
        if call.message.chat.type!='private': return
        missing=managed_unjoined(bot_db_id,call.from_user.id,mbot)
        if missing:
            mbot.answer_callback_query(call.id,'❌ सभी चैनल join करें!',show_alert=True)
            managed_start_page(mbot,bot_db_id,call.from_user)
        else:
            mbot.answer_callback_query(call.id,'✅ Verified!',show_alert=True)
            managed_start_page(mbot,bot_db_id,call.from_user)

    # IMPORTANT: Reply buttons on Live Chat messages are sent by the
    # connected bot, so their callback_query MUST be handled by that same
    # connected bot (not by the Controller Bot).
    @mbot.callback_query_handler(func=lambda call: (call.data or '').startswith(f'mreply_{bot_db_id}_'))
    def _managed_reply(call):
        try:
            if call.message.chat.type != 'private':
                return
            parts=(call.data or '').split('_',2)
            if len(parts) != 3:
                return
            uid=int(parts[2])
            if call.from_user.id != OWNER_ID_FOR_BOT(bot_db_id):
                mbot.answer_callback_query(call.id,'❌ Owner only.',show_alert=True)
                return
            mbot.answer_callback_query(call.id)
            prompt=mbot.send_message(
                call.message.chat.id,
                '↩️ <b>Reply to User</b>\n\nApna reply message bhejiye. Text reply abhi supported hai.'
            )
            mbot.register_next_step_handler(prompt, managed_send_reply_text_connected, bot_db_id, uid)
        except Exception as e:
            print('managed reply callback:', e)

    @mbot.chat_join_request_handler()
    def _join_request(req):
        try:
            if req.chat.type not in ('channel','supergroup'): return
            mdb("""INSERT INTO managed_join_requests(bot_id,user_id,channel_id) VALUES(%s,%s,%s)
                 ON CONFLICT DO NOTHING""",(bot_db_id,req.from_user.id,str(req.chat.id)),commit=True)
            managed_add_user(bot_db_id,req.from_user)
            managed_autodm(mbot,bot_db_id,req.from_user.id,req.from_user)
        except Exception as e: print('managed join request:',e)

    @mbot.chat_member_handler()
    def _member_update(update):
        try:
            chat_type=getattr(update.chat,'type','')
            if chat_type not in ('channel','supergroup'): return
            cid=str(update.chat.id)
            configured=any(str(ch[1]) == cid for ch in managed_channels(bot_db_id))
            if not configured: return

            old=getattr(update.old_chat_member,'status','')
            new=getattr(update.new_chat_member,'status','')
            user=getattr(update.new_chat_member,'user',None)
            if not user: return

            print(f'DMOnLeave event: bot={bot_db_id} chat={cid} type={chat_type} user={user.id} old={old} new={new}')

            if new in ('member','administrator','creator') and old in ('left','kicked','restricted',''):
                managed_add_user(bot_db_id,user)
                managed_autodm(mbot,bot_db_id,user.id,user)
                return

            if new == 'left' and old in ('member','administrator','creator','restricted'):
                managed_dm_on_leave(mbot,bot_db_id,user.id,user)
                return

            if new == 'kicked':
                try:
                    mdb("UPDATE managed_users SET status='blocked',last_seen_at=CURRENT_TIMESTAMP WHERE bot_id=%s AND user_id=%s",
                        (bot_db_id,user.id),commit=True)
                except Exception:
                    pass
        except Exception as e:
            print('managed member update error:',e)

    # Fallback for supergroups: Telegram may also deliver a service message.
    @mbot.message_handler(content_types=['new_chat_members','left_chat_member'])
    def _member_service(message):
        try:
            if getattr(message.chat,'type','') != 'supergroup': return
            cid=str(message.chat.id)
            if not any(str(ch[1]) == cid for ch in managed_channels(bot_db_id)): return
            user=getattr(message,'left_chat_member',None)
            if not user: return
            print(f'DMOnLeave service event: bot={bot_db_id} chat={cid} user={user.id}')
            managed_dm_on_leave(mbot,bot_db_id,user.id,user)
        except Exception as e:
            print('managed left service update error:',e)

    @mbot.message_handler(func=lambda m: m.chat.type=='private' and not (m.text or '').startswith('/'), content_types=['text','photo','video','document','sticker','voice','audio','animation','contact','location'])
    def _live(message):
        managed_add_user(bot_db_id,message.from_user)
        if message.from_user.id == OWNER_ID_FOR_BOT(bot_db_id): return
        if managed_blocked(bot_db_id,message.from_user.id): return
        try:
            copied=mbot.copy_message(OWNER_ID_FOR_BOT(bot_db_id),message.chat.id,message.message_id)
            mdb("INSERT INTO managed_live_map(bot_id,admin_message_id,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(bot_db_id,copied.message_id,message.from_user.id),commit=True)
            try:
                owner=OWNER_ID_FOR_BOT(bot_db_id)
                rm=types.InlineKeyboardMarkup()
                rm.add(types.InlineKeyboardButton('↩️ Reply to User',callback_data=f'mreply_{bot_db_id}_{message.from_user.id}'))
                mbot.send_message(owner,f'🤖 <b>{mbot.get_me().first_name}</b>\n👤 <b>{message.from_user.first_name or "User"}</b>\n🆔 <code>{message.from_user.id}</code>',reply_to_message_id=copied.message_id,reply_markup=rm,parse_mode='HTML')
            except Exception: pass
            # Confirm delivery to the connected-bot user, then remove it automatically.
            try:
                ok=mbot.send_message(message.chat.id,'✉️ <b>MESSAGE SENT SUCCESSFULLY</b>',parse_mode='HTML')
                threading.Timer(3.0,lambda: _safe_delete(mbot,message.chat.id,ok.message_id)).start()
            except Exception as e: print('managed user success notice:',e)
        except Exception as e: print('managed live chat:',e)

def OWNER_ID_FOR_BOT(bot_db_id):
    row=managed_info(bot_db_id)
    return int(row[1]) if row else ADMIN_ID

def run_managed_bot(bot_db_id, token):
    try:
        mbot=telebot.TeleBot(token,parse_mode='HTML')
        me=mbot.get_me()
        mdb("""UPDATE managed_bots
               SET bot_id=%s,bot_username=%s,bot_name=%s,status='active'
               WHERE id=%s""",
            (me.id,me.username or '',me.first_name or '',bot_db_id),commit=True)
        ensure_managed_defaults(bot_db_id)
        MANAGED_BOTS[bot_db_id]=mbot
        MANAGED_STOPS[bot_db_id]=threading.Event()
        managed_register_handlers(mbot,bot_db_id)
        print(f'Managed bot @{me.username or me.first_name} started (id={bot_db_id})')

        try:
            mbot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True,
                allowed_updates=['message','callback_query','chat_join_request','chat_member','my_chat_member']
            )
        except Exception as e:
            # 401/Unauthorized or invalid-token errors mean the BotFather token
            # is no longer usable. Mark it disconnected, but keep all DB data.
            msg=str(e)
            lower=msg.lower()
            if ('401' in msg or 'unauthorized' in lower or 'token is invalid' in lower
                    or 'bot token is invalid' in lower):
                mdb("UPDATE managed_bots SET status='disconnected' WHERE id=%s",(bot_db_id,),commit=True)
                print(f'Managed bot {bot_db_id} disconnected: invalid/revoked token')
            else:
                mdb("UPDATE managed_bots SET status='error' WHERE id=%s",(bot_db_id,),commit=True)
                print(f'Managed bot {bot_db_id} polling error:',e)
        finally:
            MANAGED_BOTS.pop(bot_db_id,None)
            MANAGED_STOPS.pop(bot_db_id,None)
            try: mbot.stop_polling()
            except Exception: pass

    except Exception as e:
        print(f'Managed bot {bot_db_id} failed:',e)
        msg=str(e).lower()
        try:
            status='disconnected' if ('401' in msg or 'unauthorized' in msg or 'invalid' in msg) else 'error'
            mdb("UPDATE managed_bots SET status=%s WHERE id=%s",(status,bot_db_id),commit=True)
        except Exception:
            pass

def ensure_manager_user(user):
    """Ensure the Telegram controller user exists before managed_bots FK insert.

    This fixes managed_bots_owner_id_fkey failures while preserving the
    existing manager_users/managed_bots relationship.
    """
    uid = int(user.id if hasattr(user, "id") else user)
    username = getattr(user, "username", "") or ""
    first_name = getattr(user, "first_name", "") or ""
    last_name = getattr(user, "last_name", "") or ""
    mdb("""
        INSERT INTO manager_users
            (user_id, username, first_name, last_name, status, updated_at)
        VALUES (%s, %s, %s, %s, 'active', CURRENT_TIMESTAMP)
        ON CONFLICT (user_id) DO UPDATE SET
            username=EXCLUDED.username,
            first_name=EXCLUDED.first_name,
            last_name=EXCLUDED.last_name,
            status='active',
            updated_at=CURRENT_TIMESTAMP
    """, (uid, username, first_name, last_name), commit=True)
    # Keep the controller's original users table synchronized as well.
    add_user(uid)


def connect_bot_prompt(chat_id):
    m=bot.send_message(chat_id,'🔗 <b>Connect Your Telegram Bot</b>\n\nBotFather से मिला पूरा token भेजें.\n\nExample:\n<code>123456789:AAxxxxxxxx</code>\n\n⚠️ Token किसी दूसरे व्यक्ति को share मत करें.')
    bot.register_next_step_handler(m,connect_bot_save)

def connect_bot_save(message):
    token=(message.text or '').strip()
    if ':' not in token:
        bot.send_message(message.chat.id,'❌ Invalid BotFather token.'); return

    owner_id=message.from_user.id
    try:
        # IMPORTANT: managed_bots.owner_id references manager_users.user_id.
        # Create/update that owner row before touching managed_bots.
        ensure_manager_user(message.from_user)
    except Exception as e:
        bot.send_message(message.chat.id, '❌ Controller user setup failed: '+str(e)[:500])
        return

    try:
        mb=telebot.TeleBot(token, parse_mode='HTML')
        me=mb.get_me()
        if not me.is_bot:
            raise ValueError('Token does not belong to a bot.')
    except Exception as e:
        bot.send_message(message.chat.id,'❌ Bot connect नहीं हुआ. Token check करें.\n<code>'+str(e)[:500]+'</code>')
        return

    try:
        # IMPORTANT: Reconnecting the same bot must reuse the existing row.
        # That preserves Start/Force Join/Broadcast/Channels/AutoDM/DMOnLeave,
        # users and live-chat data.
        existing=mdb(
            "SELECT id,owner_id,status FROM managed_bots WHERE bot_id=%s LIMIT 1",
            (me.id,), one=True
        )

        if existing:
            bid, old_owner, old_status = existing
            if int(old_owner or 0) != int(owner_id):
                bot.send_message(message.chat.id,
                    '❌ यह bot पहले किसी दूसरे controller user से connected है.')
                return

            mdb("""UPDATE managed_bots
                   SET bot_token=%s, token_ciphertext=%s,
                       bot_username=%s, bot_name=%s, status='active'
                   WHERE id=%s AND owner_id=%s""",
                (token, token, me.username or '', me.first_name or '', bid, owner_id),
                commit=True)
            ensure_managed_defaults(bid)

            # Stop any stale runtime, then start the same bot again.
            old=MANAGED_BOTS.pop(bid, None)
            MANAGED_STOPS.pop(bid, None)
            try:
                if old:
                    old.stop_polling()
            except Exception:
                pass
            threading.Thread(target=run_managed_bot,args=(bid,token),daemon=True).start()

            bot.send_message(
                message.chat.id,
                f'♻️ <b>@{me.username or me.first_name}</b> reconnected successfully! 🎉\n\n'
                'Your previous settings and data have been restored automatically.\n'
                'Nothing was reset.',
                reply_markup=types.InlineKeyboardMarkup().add(
                    types.InlineKeyboardButton('🤖 Open Control Panel',callback_data=f'mbot_{bid}'),
                    types.InlineKeyboardButton('🤖 My Connected Bots',callback_data='my_bots')
                )
            )
            return

        # New bot.
        row=mdb("""INSERT INTO managed_bots
                   (owner_id,owner_user_id,bot_token,token_ciphertext,bot_id,
                    bot_username,bot_name,status)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,'active')
                   RETURNING id""",
                (owner_id,owner_id,token,token,me.id,
                 me.username or '',me.first_name or ''),one=True,commit=True)
        bid=row[0]
        ensure_managed_defaults(bid)
        threading.Thread(target=run_managed_bot,args=(bid,token),daemon=True).start()

        bot.send_message(
            message.chat.id,
            f'✅ <b>@{me.username or me.first_name}</b> connected successfully! 🎉\n\n'
            'Your bot is now ready to control.\n'
            'All settings and data will be stored separately for this bot.',
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton('🤖 Open Control Panel',callback_data=f'mbot_{bid}'),
                types.InlineKeyboardButton('🤖 My Connected Bots',callback_data='my_bots')
            )
        )
    except Exception as e:
        bot.send_message(message.chat.id,'❌ Connect failed: '+str(e)[:500])

def show_managed_bots(chat_id):
    rows=owner_bot_ids(chat_id)
    mk=types.InlineKeyboardMarkup(row_width=1)
    if rows:
        for rid,username,name,status in rows:
            live=(status=='active' and rid in MANAGED_BOTS)
            if live:
                state='🟢 Connected'
            elif status=='disconnected':
                state='🔴 Disconnected'
            else:
                state='🟠 Error / Reconnect'
            label='🤖 @'+(username or name or str(rid))+' • '+state
            mk.add(types.InlineKeyboardButton(label,callback_data=f'mbot_{rid}'))
    mk.add(types.InlineKeyboardButton('➕ Connect Bot',callback_data='connect_bot'))
    mk.add(types.InlineKeyboardButton('⬅️ Back',callback_data='controller_back'))
    bot.send_message(chat_id,'🤖 <b>My Connected Bots</b>\n\nSelect a bot to manage it.',reply_markup=mk)

def managed_home(chat_id,bot_db_id):
    row=managed_info(bot_db_id)
    if not row or int(row[1] or 0)!=chat_id:
        return

    status=(row[6] or 'disconnected')
    live = status == 'active' and bot_db_id in MANAGED_BOTS
    status_text='🟢 Connected & Running' if live else '🔴 Disconnected — Reconnect your bot'

    mk=types.InlineKeyboardMarkup(row_width=2)
    for title,data in [
        ('⚙️ Start',f'ms_{bot_db_id}_start'),
        ('🔐 Force Join',f'ms_{bot_db_id}_fj'),
        ('📢 Broadcast',f'ms_{bot_db_id}_bc'),
        ('📺 Channels',f'ms_{bot_db_id}_ch'),
        ('🤖 AutoDM',f'ms_{bot_db_id}_dm'),
        ('👋 DMOnLeave',f'ms_{bot_db_id}_leave'),
        ('📊 Statistics',f'ms_{bot_db_id}_stats'),
        ('💬 Live Chat',f'ms_{bot_db_id}_live'),
        ('👥 Users',f'ms_{bot_db_id}_users')
    ]:
        mk.add(types.InlineKeyboardButton(title,callback_data=data))

    if live:
        mk.add(types.InlineKeyboardButton('🔴 Disconnect',callback_data=f'mdisconnect_{bot_db_id}'))
    else:
        mk.add(types.InlineKeyboardButton('♻️ Reconnect Bot',callback_data=f'mreconnect_{bot_db_id}'))

    mk.add(types.InlineKeyboardButton('⬅️ My Bots',callback_data='my_bots'))
    bot.send_message(
        chat_id,
        f'🤖 <b>@{row[4] or row[5]}</b>\n'
        f'📡 Status: <b>{status_text}</b>\n\n'
        'Control panel — all settings and data are stored separately for this bot.',
        reply_markup=mk
    )

def managed_settings_keyboard(bot_db_id,prefix,next_cb):
    mk=types.InlineKeyboardMarkup(row_width=2)
    mk.add(types.InlineKeyboardButton('🖼️ Media',callback_data=f'medit_{bot_db_id}_{prefix}_media'),types.InlineKeyboardButton('👀 See',callback_data=f'msee_{bot_db_id}_{prefix}_media'))
    mk.add(types.InlineKeyboardButton('📝 Text',callback_data=f'medit_{bot_db_id}_{prefix}_text'),types.InlineKeyboardButton('👀 See',callback_data=f'msee_{bot_db_id}_{prefix}_text'))
    mk.add(types.InlineKeyboardButton('🔘 Buttons',callback_data=f'medit_{bot_db_id}_{prefix}_buttons'),types.InlineKeyboardButton('👀 See',callback_data=f'msee_{bot_db_id}_{prefix}_buttons'))
    pin=mget(bot_db_id,prefix+'_pin')=='YES'
    mk.add(types.InlineKeyboardButton('📌 Pin',callback_data=f'mpin_{bot_db_id}_{prefix}'),types.InlineKeyboardButton('✅ YES' if pin else '❌ NO',callback_data=f'mpin_{bot_db_id}_{prefix}'))
    mk.add(types.InlineKeyboardButton('👀 Full Preview',callback_data=f'mpreview_{bot_db_id}_{prefix}'))
    mk.add(types.InlineKeyboardButton('⬅️ Back',callback_data=f'mbot_{bot_db_id}'),types.InlineKeyboardButton('Next ➡️',callback_data=next_cb))
    return mk

def managed_settings_menu(chat_id,bot_db_id,prefix,title,next_cb):
    bot.send_message(chat_id,f'{title}\n\nMedia → photo/video\nText → message text\nButtons → one per line: <code>#g Join | https://t.me/example</code>',reply_markup=managed_settings_keyboard(bot_db_id,prefix,next_cb))

def managed_save_input(message,bot_db_id,prefix,kind):
    if not managed_is_owner(bot_db_id,message.from_user.id):
        bot.send_message(message.chat.id,'❌ यह आपका connected bot नहीं है.')
        return
    if kind=='text':
        if not message.text: bot.send_message(message.chat.id,'❌ Text भेजें.'); return
        _store_formatted_setting(lambda k,v: mset(bot_db_id,k,v), prefix+'_text', message)
    elif kind=='buttons':
        if not message.text: bot.send_message(message.chat.id,'❌ Buttons text भेजें.'); return
        mset(bot_db_id,prefix+'_buttons',message.text)
    elif kind=='media':
        if message.text and message.text.strip().upper()=='NONE':
            mset(bot_db_id,prefix+'_media_id','NONE'); mset(bot_db_id,prefix+'_media_type','NONE')
        elif message.photo or message.video:
            mbot=MANAGED_BOTS.get(bot_db_id)
            if not mbot:
                bot.send_message(message.chat.id,'❌ Connected bot running नहीं है.'); return
            try:
                owner=OWNER_ID_FOR_BOT(bot_db_id)
                src_id=message.photo[-1].file_id if message.photo else message.video.file_id
                f=bot.get_file(src_id)
                data=bot.download_file(f.file_path)
                bio=io.BytesIO(data); bio.name='upload.bin'
                if message.photo:
                    uploaded=mbot.send_photo(owner,bio)
                    managed_id=uploaded.photo[-1].file_id; managed_type='photo'
                else:
                    uploaded=mbot.send_video(owner,bio)
                    managed_id=uploaded.video.file_id; managed_type='video'
                try: mbot.delete_message(owner,uploaded.message_id)
                except Exception: pass
                mset(bot_db_id,prefix+'_media_id',managed_id); mset(bot_db_id,prefix+'_media_type',managed_type)
            except Exception as e:
                bot.send_message(message.chat.id,'❌ Media upload failed. पहले connected bot को /start करें, फिर media दोबारा भेजें.\n'+str(e)[:250]); return
        else:
            bot.send_message(message.chat.id,'❌ Photo, video या NONE भेजें.'); return
    bot.send_message(message.chat.id,'✅ Setting saved.')
    managed_settings_menu(message.chat.id,bot_db_id,prefix,{'start':'⚙️ Start Message','fj':'🔐 Force Join','bcast':'📢 Broadcast','autodm':'🤖 AutoDM','dmleave':'👋 DMOnLeave Settings'}[prefix],f'mnext_{bot_db_id}_{prefix}')

def managed_preview(chat_id,bot_db_id,prefix):
    mbot=MANAGED_BOTS.get(bot_db_id)
    if not mbot: bot.send_message(chat_id,'❌ Connected bot is not running.'); return
    # Preview is sent by the connected bot into the controller chat, matching the live bot payload.
    managed_send_payload(mbot,chat_id,bot_db_id,prefix,False)

def managed_broadcast(bot_db_id,owner_chat_id):
    mbot=MANAGED_BOTS.get(bot_db_id)
    if not mbot: bot.send_message(owner_chat_id,'❌ Bot is not running.'); return
    rows=mdb("SELECT user_id FROM managed_users WHERE bot_id=%s AND status='active'",(bot_db_id,),all_rows=True) or []
    sent=failed=0
    for (uid,) in rows:
        try: managed_send_payload(mbot,uid,bot_db_id,'bcast',False); sent+=1
        except Exception: failed+=1
        time.sleep(0.03)
    bot.send_message(owner_chat_id,f'📢 <b>Broadcast finished</b>\n\n✅ Sent: {sent}\n❌ Failed: {failed}')

def managed_send_reply_text_connected(message,bot_db_id,user_id):
    """Send an owner reply through the connected bot, preserving Telegram formatting."""
    mbot=MANAGED_BOTS.get(bot_db_id)
    if not mbot or not message.text:
        try:
            if mbot: mbot.send_message(message.chat.id,'❌ Connected bot unavailable या text नहीं मिला.')
        except Exception: pass
        return
    try:
        txt=_message_html(message)
        mbot.send_message(user_id,txt,parse_mode='HTML')
        mbot.send_message(message.chat.id,'✅ Reply sent.')
    except Exception as e:
        try: mbot.send_message(message.chat.id,'❌ Reply failed: '+str(e)[:300])
        except Exception: pass

def managed_send_reply_text(message,bot_db_id,user_id):
    mbot=MANAGED_BOTS.get(bot_db_id)
    if not mbot or not message.text:
        bot.send_message(message.chat.id,'❌ Connected bot unavailable या text नहीं मिला.')
        return
    try:
        mbot.send_message(user_id,message.text)
        bot.send_message(message.chat.id,'✅ Reply sent.')
    except Exception as e:
        bot.send_message(message.chat.id,'❌ Reply failed: '+str(e)[:300])

def managed_add_channel_prompt(chat_id,bot_db_id):
    m=bot.send_message(chat_id,'➕ Channel जोड़ें:\n\n<code>-1001234567890 | #g Channel Name | https://t.me/example</code>\n\nConnected bot उस channel का admin होना चाहिए.')
    bot.register_next_step_handler(m,managed_add_channel,bot_db_id)

def managed_add_channel(message,bot_db_id):
    try:
        parts=[x.strip() for x in (message.text or '').split('|')]
        if len(parts)!=3: raise ValueError()
        cid,title,link=parts
        mbot=MANAGED_BOTS.get(bot_db_id)
        if not mbot: raise ValueError('Bot is not running')
        mbot.get_chat(cid)
        me=mbot.get_me()
        bot_member=mbot.get_chat_member(cid,me.id)
        if getattr(bot_member,'status','') not in ('administrator','creator'):
            raise ValueError('Connected bot must be an administrator in this channel/group for DMOnLeave updates.')
        mdb("INSERT INTO managed_channels(bot_id,channel_id,title,link) VALUES(%s,%s,%s,%s) ON CONFLICT(bot_id,channel_id) DO UPDATE SET title=EXCLUDED.title,link=EXCLUDED.link",(bot_db_id,cid,title,link),commit=True)
        bot.send_message(message.chat.id,'✅ Channel added. Bot admin rights verified.'); managed_channels_menu(message.chat.id,bot_db_id)
    except Exception as e: bot.send_message(message.chat.id,'❌ Channel add failed. Bot admin rights और format check करें.\n'+str(e)[:300])

def managed_channels_menu(chat_id,bot_db_id):
    rows=managed_channels(bot_db_id); mk=types.InlineKeyboardMarkup(row_width=1)
    for rid,cid,title,link in rows: mk.add(types.InlineKeyboardButton('🗑️ '+(title or cid),callback_data=f'mchdel_{bot_db_id}_{rid}'))
    mk.add(types.InlineKeyboardButton('➕ Add Channel',callback_data=f'mchadd_{bot_db_id}'),types.InlineKeyboardButton('⬅️ Back',callback_data=f'mbot_{bot_db_id}'))
    bot.send_message(chat_id,'📺 <b>Managed Bot Channels</b>\n\nइन channels पर Force Join और AutoDM events काम करेंगे.',reply_markup=mk)

def managed_stats(chat_id,bot_db_id):
    row=mdb("SELECT COUNT(*),COUNT(*) FILTER(WHERE status='active'),COUNT(*) FILTER(WHERE status='blocked') FROM managed_users WHERE bot_id=%s",(bot_db_id,),one=True) or (0,0,0)
    ch=len(managed_channels(bot_db_id))
    bot.send_message(chat_id,f'📊 <b>Bot Statistics</b>\n\n👥 Total users: {row[0]}\n🟢 Active: {row[1]}\n🔴 Blocked: {row[2]}\n📺 Channels: {ch}',reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('⬅️ Back',callback_data=f'mbot_{bot_db_id}')))

def managed_users_menu(chat_id,bot_db_id):
    rows=mdb("SELECT user_id,first_name,username,status FROM managed_users WHERE bot_id=%s ORDER BY last_seen_at DESC LIMIT 50",(bot_db_id,),all_rows=True) or []
    text='👥 <b>Recent Users</b>\n\n'+('\n'.join(f'<code>{r[0]}</code> • {r[1] or "User"} • {r[3]}' for r in rows) if rows else 'No users yet.')
    bot.send_message(chat_id,text,reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('⬅️ Back',callback_data=f'mbot_{bot_db_id}')))

def managed_live_menu(chat_id,bot_db_id):
    bot.send_message(chat_id,'💬 <b>Live Chat</b>\n\nUsers who message this connected bot are copied here. Reply to a copied message to answer them.',reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('⬅️ Back',callback_data=f'mbot_{bot_db_id}')))


# ============================================================
# CALLBACKS
# ============================================================
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    chat_id = call.message.chat.id
    user_id = call.from_user.id

    # --------------------------------------------------------
    # Force Join Verification
    # --------------------------------------------------------
    # CONTROLLER ADMIN GLOBAL VIEW
    # Only ADMIN_ID can see global controller users/bots/statistics.
    # Regular users never reach these handlers.
    # --------------------------------------------------------
    if call.data == "controller_admin":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Admin only", show_alert=True); return
        bot.answer_callback_query(call.id)
        show_admin_main(chat_id)
        return

    if call.data == "controller_stats":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Admin only", show_alert=True); return
        bot.answer_callback_query(call.id)
        with DB_LOCK:
            conn=db_connect(); c=conn.cursor()
            c.execute("SELECT COUNT(*) FROM users"); total_users=c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE status='active'"); active_users=c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM users WHERE status='blocked'"); blocked_users=c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM managed_bots WHERE status='active'"); active_bots=c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM managed_bots"); total_bots=c.fetchone()[0]
            c.execute("SELECT COUNT(DISTINCT owner_id) FROM managed_bots"); bot_owners=c.fetchone()[0]
            conn.close()
        bot.send_message(chat_id,
            "📈 <b>Controller Statistics</b>\n\n"
            f"👥 Total users: <b>{total_users}</b>\n"
            f"🟢 Active users: <b>{active_users}</b>\n"
            f"🔴 Blocked users: <b>{blocked_users}</b>\n\n"
            f"🤖 Total connected bots: <b>{total_bots}</b>\n"
            f"🟢 Active connected bots: <b>{active_bots}</b>\n"
            f"👤 Users with connected bots: <b>{bot_owners}</b>")
        return

    if call.data == "controller_users":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Admin only", show_alert=True); return
        bot.answer_callback_query(call.id)
        rows=db_execute("SELECT user_id,status FROM users ORDER BY user_id DESC LIMIT 100",fetchall=True) or []
        if not rows:
            bot.send_message(chat_id,"👥 <b>Controller Users</b>\n\nNo users yet."); return
        lines=["👥 <b>Controller Users</b>", ""]
        for uid,st in rows:
            count=db_execute("SELECT COUNT(*) FROM managed_bots WHERE owner_id=%s",(uid,),fetchone=True)[0]
            lines.append(f"• <code>{uid}</code> — {('🟢' if st=='active' else '🔴')} — 🤖 {count} bot(s)")
        bot.send_message(chat_id,"\n".join(lines))
        return

    if call.data == "controller_bots":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "❌ Admin only", show_alert=True); return
        bot.answer_callback_query(call.id)
        rows=db_execute("""SELECT id,owner_id,bot_username,bot_name,status FROM managed_bots ORDER BY id DESC LIMIT 100""",fetchall=True) or []
        if not rows:
            bot.send_message(chat_id,"🤖 <b>All Connected Bots</b>\n\nNo connected bots yet."); return
        lines=["🤖 <b>All Connected Bots</b>", ""]
        for bid,owner,uname,name,st in rows:
            label='@'+(uname or name or str(bid))
            lines.append(f"• <b>{label}</b> | Owner: <code>{owner}</code> | {('🟢' if st=='active' else '🔴')}")
        bot.send_message(chat_id,"\n".join(lines))
        return

    # --------------------------------------------------------
    if call.data == "verify_force_join":
        unjoined = get_unjoined_channels(user_id)

        if not unjoined:
            bot.answer_callback_query(
                call.id,
                "✅ सभी चैनल ज्वाइन हैं! Welcome!",
                show_alert=True
            )

            try:
                bot.delete_message(
                    chat_id,
                    call.message.message_id
                )
            except Exception:
                pass

            # Welcome is allowed ONLY after the fresh Force-Join check above.
            send_start_page(
                chat_id,
                call.from_user
            )

        else:
            bot.answer_callback_query(
                call.id,
                "❌ सभी चैनल ज्वाइन करें!",
                show_alert=True
            )

            send_force_join_page(
                chat_id,
                call.from_user
            )

        return

    # --------------------------------------------------------
    # Connected Bot Controller
    # IMPORTANT: My Bots / Connect Bot are NOT admin-only.
    # Every user of this controller can connect and manage only their own bots.
    # --------------------------------------------------------
    if call.data == 'my_bots':
        bot.answer_callback_query(call.id)
        show_managed_bots(chat_id)
        return
    if call.data == 'connect_bot':
        bot.answer_callback_query(call.id)
        connect_bot_prompt(chat_id)
        return
    if call.data == 'controller_back':
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, '🤖 <b>Multi-Bot Controller</b>\nUse <b>My Bots</b> to manage your connected bots.', reply_markup=types.InlineKeyboardMarkup().add(types.InlineKeyboardButton('🤖 My Bots', callback_data='my_bots')))
        return
    if call.data.startswith('mbot_'):
        bid=int(call.data.split('_',1)[1])
        if not managed_is_owner(bid,user_id):
            bot.answer_callback_query(call.id,'❌ यह आपका connected bot नहीं है.',show_alert=True); return
        bot.answer_callback_query(call.id)
        managed_home(chat_id,bid); return
    if call.data.startswith('mdisconnect_'):
        bid=int(call.data.split('_',1)[1])
        if managed_is_owner(bid,user_id):
            mbot=MANAGED_BOTS.pop(bid,None)
            MANAGED_STOPS.pop(bid,None)
            try:
                if mbot:
                    mbot.stop_polling()
            except Exception:
                pass
            # Keep the row and every setting/data row. Only the runtime status changes.
            mdb("UPDATE managed_bots SET status='disconnected' WHERE id=%s AND owner_id=%s",
                (bid,user_id),commit=True)
            bot.answer_callback_query(call.id,'Bot disconnected')
            show_managed_bots(chat_id)
        else:
            bot.answer_callback_query(call.id,'❌ यह आपका bot नहीं है.',show_alert=True)
        return

    if call.data.startswith('mreconnect_'):
        bid=int(call.data.split('_',1)[1])
        if not managed_is_owner_any_status(bid,user_id):
            bot.answer_callback_query(call.id,'❌ यह आपका connected bot नहीं है.',show_alert=True)
            return
        bot.answer_callback_query(call.id)
        mrow=managed_info_any(bid)
        if not mrow:
            return
        prompt=bot.send_message(
            chat_id,
            '♻️ <b>Reconnect Your Bot</b>\n\n'
            'इस bot का नया/current BotFather token भेजें.\n'
            'पुराना Start, Broadcast, Channels, AutoDM, DMOnLeave और बाकी data सुरक्षित रहेगा।'
        )
        bot.register_next_step_handler(prompt, lambda msg: reconnect_existing_bot(msg,bid))
        return

    # The connected-bot panel is a user-owned area, not controller-admin-only.
    # Ownership is checked for every connected-bot operation below.
    # Only the original controller bot's own admin settings are protected by
    # the admin gate that appears after this block.

    # --------------------------------------------------------
    # Connected Bot Controller (owner-only operations)
    # --------------------------------------------------------
    if call.data.startswith('ms_'):
        _,bid_s,action=call.data.split('_',2); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        bot.answer_callback_query(call.id)
        if action=='start': managed_settings_menu(chat_id,bid,'start','⚙️ <b>Start Message</b>',f'mnext_{bid}_start')
        elif action=='fj': managed_settings_menu(chat_id,bid,'fj','🔐 <b>Force Join</b>',f'mnext_{bid}_fj')
        elif action=='bc': managed_settings_menu(chat_id,bid,'bcast','📢 <b>Broadcast</b>',f'mnext_{bid}_bcast')
        elif action=='dm': managed_settings_menu(chat_id,bid,'autodm','🤖 <b>AutoDM</b>',f'mnext_{bid}_autodm')
        elif action=='leave': managed_settings_menu(chat_id,bid,'dmleave','👋 <b>DMOnLeave Settings</b>',f'mnext_{bid}_dmleave')
        elif action=='ch': managed_channels_menu(chat_id,bid)
        elif action=='stats': managed_stats(chat_id,bid)
        elif action=='users': managed_users_menu(chat_id,bid)
        elif action=='live': managed_live_menu(chat_id,bid)
        return
    if call.data.startswith('medit_'):
        _,bid_s,prefix,kind=call.data.split('_',3); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        prompt={'media':'🖼️ Photo/video भेजें या NONE','text':'📝 Text भेजें','buttons':'🔘 Buttons भेजें (हर line: #g Title | https://example.com)'}[kind]
        m=bot.send_message(chat_id,prompt); bot.register_next_step_handler(m,managed_save_input,bid,prefix,kind); return
    if call.data.startswith('msee_'):
        _,bid_s,prefix,kind=call.data.split('_',3); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        if kind=='media': val=mget(bid,prefix+'_media_type')
        elif kind=='text': val=_render_template(_get_formatted_setting(mget,prefix+'_text'), managed_user_for_chat(bid,chat_id))
        else: val=mget(bid,prefix+'_'+kind)
        bot.send_message(chat_id,f'👀 <b>{prefix} {kind}</b>\n\n{val}',parse_mode='HTML'); return
    if call.data.startswith('mpin_'):
        _,bid_s,prefix=call.data.split('_',2); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        key=prefix+'_pin'; mset(bid,key,'NO' if mget(bid,key)=='YES' else 'YES'); bot.answer_callback_query(call.id,'Pin setting updated'); managed_settings_menu(chat_id,bid,prefix,{'start':'⚙️ <b>Start Message</b>','fj':'🔐 <b>Force Join</b>','bcast':'📢 <b>Broadcast</b>','autodm':'🤖 <b>AutoDM</b>','dmleave':'👋 <b>DMOnLeave Settings</b>'}[prefix],f'mnext_{bid}_{prefix}'); return
    if call.data.startswith('mpreview_'):
        _,bid_s,prefix=call.data.split('_',2); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        managed_preview(chat_id,bid,prefix); return
    if call.data.startswith('mreply_'):
        _,bid_s,uid_s=call.data.split('_',2); bid=int(bid_s); uid=int(uid_s)
        if not managed_is_owner(bid,user_id): return
        m=bot.send_message(chat_id,'↩️ Reply भेजें. अभी text reply support है; message भेजते ही connected bot user को भेज दिया जाएगा.')
        bot.register_next_step_handler(m,managed_send_reply_text,bid,uid)
        return
    if call.data.startswith('mnext_'):
        _,bid_s,prefix=call.data.split('_',2); bid=int(bid_s)
        if not managed_is_owner(bid,user_id): return
        if prefix=='bcast':
            bot.answer_callback_query(call.id,'Broadcast started')
            threading.Thread(target=managed_broadcast,args=(bid,chat_id),daemon=True).start()
        elif prefix=='autodm':
            mset(bid,'autodm_enabled','YES'); bot.answer_callback_query(call.id,'AutoDM saved'); managed_home(chat_id,bid)
        elif prefix=='dmleave':
            mset(bid,'dmleave_enabled','YES'); bot.answer_callback_query(call.id,'DMOnLeave saved'); managed_home(chat_id,bid)
        else:
            bot.answer_callback_query(call.id,'Settings saved'); managed_home(chat_id,bid)
        return
    if call.data.startswith('mchadd_'):
        bid=int(call.data.split('_',1)[1])
        if not managed_is_owner(bid,user_id):
            bot.answer_callback_query(call.id,'❌ यह आपका connected bot नहीं है.',show_alert=True)
            return
        bot.answer_callback_query(call.id)
        managed_add_channel_prompt(chat_id,bid)
        return
    if call.data.startswith('mchdel_'):
        _,bid_s,rid_s=call.data.split('_',2); bid=int(bid_s); rid=int(rid_s)
        if managed_is_owner(bid,user_id):
            mdb('DELETE FROM managed_channels WHERE id=%s AND bot_id=%s',(rid,bid),commit=True); managed_channels_menu(chat_id,bid)
        return

    # --------------------------------------------------------
    # Controller admin-only area starts here
    # --------------------------------------------------------
    if not is_admin(user_id):
        bot.answer_callback_query(call.id,"❌ Admin only")
        return

    # --------------------------------------------------------
    # Admin Home
    # --------------------------------------------------------
    if call.data == "adm_home":
        bot.answer_callback_query(call.id)
        show_admin_main(chat_id)

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------
    elif call.data == "menu_stats":
        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()

            c.execute("SELECT COUNT(*) FROM users")
            tot = c.fetchone()[0]

            c.execute(
                "SELECT COUNT(*) FROM users WHERE status='active'"
            )
            act = c.fetchone()[0]

            c.execute(
                "SELECT COUNT(*) FROM users WHERE status='blocked'"
            )
            blk = c.fetchone()[0]

            conn.close()

        txt = (
            "📊 <b>बोट आंकड़े:</b>\n\n"
            f"👥 कुल यूज़र्स: {tot}\n"
            f"🟢 सक्रिय: {act}\n"
            f"🔴 ब्लॉक: {blk}"
        )

        bot.send_message(chat_id, txt)

    # --------------------------------------------------------
    # START SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_start":
        bot.send_message(
            chat_id,
            "⚙️ <b>Start Settings</b>",
            reply_markup=get_modular_keyboard("st")
        )

    elif call.data == "st_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ फोटो/वीडियो भेजें (या NONE लिखें):"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "start"
        )

    elif call.data == "st_see_media":
        bot.send_message(
            chat_id,
            "🖼️ Media Type: " +
            str(get_set("start_media_type"))
        )

    elif call.data == "st_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ Start Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_start_text
        )

    elif call.data == "st_see_text":
        bot.send_message(
            chat_id,
            "📝 टेक्स्ट:\n\n" +
            str(get_set("start_text"))
        )

    elif call.data == "st_edit_btn":
        guide = (
            "🔘 बटन लिखें (हर बटन नई लाइन में):\n\n"
            "<code>#r Join | https://t.me/example</code>\n"
            "<code>#g Main Channel | https://t.me/example</code>"
        )

        m = bot.send_message(
            chat_id,
            guide
        )

        bot.register_next_step_handler(
            m,
            save_start_buttons
        )

    elif call.data == "st_see_btn":
        bot.send_message(
            chat_id,
            "🔘 बटन:\n\n" +
            str(get_set("start_buttons"))
        )

    elif call.data == "st_toggle_pin":
        cur = get_set("start_pin")
        set_val(
            "start_pin",
            "NO" if cur == "YES" else "YES"
        )

        bot.send_message(
            chat_id,
            "📌 पिन: " +
            str(get_set("start_pin"))
        )

    elif call.data == "st_full_prev":
        send_start_page(
            chat_id,
            call.from_user
        )

    # --------------------------------------------------------
    # FORCE JOIN SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_fj":
        bot.send_message(
            chat_id,
            "🔐 <b>Force Join Settings</b>",
            reply_markup=get_fj_keyboard()
        )

    elif call.data == "fj_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ FJ मीडिया भेजें:"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "fj"
        )

    elif call.data == "fj_see_media":
        bot.send_message(
            chat_id,
            "🖼️ FJ Media Type: " +
            str(get_set("fj_media_type"))
        )

    elif call.data == "fj_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ FJ Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_fj_text
        )

    elif call.data == "fj_see_text":
        bot.send_message(
            chat_id,
            "📝 FJ Text:\n\n" +
            str(get_set("fj_text_html") or get_set("fj_text"))
        )

    elif call.data == "fj_edit_btn":
        guide = (
            "🔘 Verify Button नाम लिखें:\n"
            "<code>#g जाँच करें / Try Again</code>"
        )

        m = bot.send_message(
            chat_id,
            guide
        )

        bot.register_next_step_handler(
            m,
            save_fj_button
        )

    elif call.data == "fj_see_btn":
        bot.send_message(
            chat_id,
            "🔘 Verify Button:\n" +
            str(get_set("fj_verify_btn"))
        )

    elif call.data == "fj_full_prev":
        send_force_join_page(
            chat_id,
            call.from_user
        )

    # --------------------------------------------------------
    # BROADCAST SETTINGS
    # --------------------------------------------------------
    elif call.data == "menu_bc":
        bot.send_message(
            chat_id,
            "📢 <b>Broadcast Settings</b>",
            reply_markup=get_modular_keyboard(
                "bc",
                is_bcast=True
            )
        )

    elif call.data == "bc_edit_media":
        m = bot.send_message(
            chat_id,
            "🖼️ प्रसारण मीडिया भेजें:"
        )
        bot.register_next_step_handler(
            m,
            save_media,
            "bcast"
        )

    elif call.data == "bc_see_media":
        bot.send_message(
            chat_id,
            "🖼️ Broadcast Media Type: " +
            str(get_set("bcast_media_type"))
        )

    elif call.data == "bc_edit_text":
        m = bot.send_message(
            chat_id,
            "✏️ Broadcast Text लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_bcast_text
        )

    elif call.data == "bc_see_text":
        bot.send_message(
            chat_id,
            "📝 Broadcast Text:\n\n" +
            str(get_set("bcast_text_html") or get_set("bcast_text"))
        )

    elif call.data == "bc_edit_btn":
        m = bot.send_message(
            chat_id,
            "🔘 Broadcast Buttons लिखें:"
        )
        bot.register_next_step_handler(
            m,
            save_bcast_buttons
        )

    elif call.data == "bc_see_btn":
        bot.send_message(
            chat_id,
            "🔘 Broadcast Buttons:\n\n" +
            str(get_set("bcast_buttons"))
        )

    elif call.data == "bc_toggle_pin":
        cur = get_set("bcast_pin")
        set_val(
            "bcast_pin",
            "NO" if cur == "YES" else "YES"
        )

        bot.send_message(
            chat_id,
            "📌 पिन: " +
            str(get_set("bcast_pin"))
        )

    elif call.data == "bc_full_prev":
        send_broadcast_preview(chat_id)

    elif call.data == "bc_next_send":
        # Run broadcast in a separate thread so the bot
        # continues receiving updates.
        bot.answer_callback_query(
            call.id,
            "🚀 Broadcast शुरू हो रहा है..."
        )

        threading.Thread(
            target=execute_broadcast,
            args=(chat_id,),
            daemon=True
        ).start()

    # --------------------------------------------------------
    # CHANNELS
    # --------------------------------------------------------
    elif call.data == "menu_channels":
        show_channels(chat_id)

    elif call.data == "add_ch_prompt":
        msg = (
            "➕ चैनल इस प्रारूप में भेजें:\n\n"
            "<code>-1001234567890 | #g चैनल नाम | https://t.me/example</code>\n\n"
            "Bot को उस channel का admin बनाना जरूरी है।"
        )

        m = bot.send_message(chat_id, msg)

        bot.register_next_step_handler(
            m,
            process_add_ch
        )

    elif call.data.startswith("del_ch_"):
        ch_db_id = call.data.replace(
            "del_ch_", ""
        )

        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()
            c.execute(
                "DELETE FROM channels WHERE id=%s",
                (ch_db_id,)
            )
            conn.commit()
            conn.close()

        bot.send_message(
            chat_id,
            "✅ चैनल हटा दिया गया!"
        )

        show_channels(chat_id)

# ============================================================
# ADMIN INPUT SAVE FUNCTIONS
# ============================================================
def save_media(message, prefix):
    if message.photo:
        set_val(
            prefix + "_media_id",
            message.photo[-1].file_id
        )
        set_val(
            prefix + "_media_type",
            "photo"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ फोटो सुरक्षित!"
        )

    elif message.video:
        set_val(
            prefix + "_media_id",
            message.video.file_id
        )
        set_val(
            prefix + "_media_type",
            "video"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ वीडियो सुरक्षित!"
        )

    elif message.text and message.text.strip().upper() == "NONE":
        set_val(
            prefix + "_media_id",
            "NONE"
        )
        set_val(
            prefix + "_media_type",
            "NONE"
        )
        bot.send_message(
            ADMIN_ID,
            "✅ मीडिया हटा दिया गया!"
        )

    else:
        bot.send_message(
            ADMIN_ID,
            "❌ कृपया photo, video या NONE भेजें।"
        )

def save_start_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    _store_formatted_setting(set_val, "start_text", message)
    bot.send_message(
        ADMIN_ID,
        "✅ Start Text अपडेट!"
    )

def save_fj_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    _store_formatted_setting(set_val, "fj_text", message)
    bot.send_message(
        ADMIN_ID,
        "✅ Force Join Text अपडेट!"
    )

def save_fj_button(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    set_val(
        "fj_verify_btn",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Verify Button अपडेट!"
    )

def save_start_buttons(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ Buttons text भेजें।"
        )
        return

    set_val(
        "start_buttons",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Start Buttons अपडेट!"
    )

def save_bcast_text(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ केवल text भेजें।"
        )
        return

    _store_formatted_setting(set_val, "bcast_text", message)

    bot.send_message(
        ADMIN_ID,
        "✅ Broadcast Text अपडेट!"
    )

def save_bcast_buttons(message):
    if not message.text:
        bot.send_message(
            ADMIN_ID,
            "❌ Buttons text भेजें।"
        )
        return

    set_val(
        "bcast_buttons",
        message.text
    )

    bot.send_message(
        ADMIN_ID,
        "✅ Broadcast Buttons अपडेट!"
    )

# ============================================================
# CHANNEL MANAGEMENT
# ============================================================
def process_add_ch(message):
    try:
        if not message.text:
            raise ValueError()

        parts = [
            x.strip()
            for x in message.text.split("|")
        ]

        if len(parts) != 3:
            raise ValueError()

        cid, title, link = parts

        if not cid or not title or not link:
            raise ValueError()

        # Validate that Telegram can access the channel.
        try:
            bot.get_chat(cid)
        except Exception:
            bot.send_message(
                ADMIN_ID,
                "⚠️ Channel ID check नहीं हो सका। "
                "सुनिश्चित करें कि Bot channel में admin है।"
            )

        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()

            c.execute(
                """
                INSERT INTO channels
                (channel_id, title, link)
                VALUES (%s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE
                SET title=EXCLUDED.title, link=EXCLUDED.link
                """,
                (cid, title, link)
            )

            conn.commit()
            conn.close()

        bot.send_message(
            ADMIN_ID,
            "✅ चैनल जुड़ गया: " +
            parse_colors(title)
        )

        show_channels(ADMIN_ID)

    except Exception:
        bot.send_message(
            ADMIN_ID,
            "❌ गलत फॉर्मेट!\n\n"
            "सही:\n"
            "<code>ID | Title | Link</code>"
        )

def show_channels(chat_id):
    channels = get_channels()

    txt = "📢 <b>चैनल सूची:</b>\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)

    if channels:
        for ch in channels:
            title = parse_colors(ch[2])

            txt += (
                f"• <b>{title}</b>\n"
                f"ID: <code>{ch[1]}</code>\n"
                f"🔗 {ch[3]}\n\n"
            )

            markup.add(
                types.InlineKeyboardButton(
                    "❌ हटाएँ " + title,
                    callback_data="del_ch_" + str(ch[0])
                )
            )

    else:
        txt += "कोई चैनल नहीं है।"

    markup.add(
        types.InlineKeyboardButton(
            "➕ नया चैनल जोड़ें",
            callback_data="add_ch_prompt"
        )
    )

    markup.add(
        types.InlineKeyboardButton(
            "🏠 Menu",
            callback_data="adm_home"
        )
    )

    bot.send_message(
        chat_id,
        txt,
        reply_markup=markup
    )

# ============================================================
# BROADCAST
# ============================================================
def send_broadcast_preview(chat_id):
    txt_template = get_set("bcast_text_html") or get_set("bcast_text")
    m_id = get_set("bcast_media_id")
    m_type = get_set("bcast_media_type")

    markup = build_keyboard_from_string(
        get_set("bcast_buttons")
    )

    try:
        if m_type == "photo" and m_id != "NONE":
            bot.send_photo(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        elif m_type == "video" and m_id != "NONE":
            bot.send_video(
                chat_id,
                m_id,
                caption=txt,
                reply_markup=markup,
                parse_mode="HTML"
            )

        else:
            bot.send_message(
                chat_id,
                txt,
                reply_markup=markup
            )

    except Exception as e:
        bot.send_message(
            chat_id,
            "❌ Preview error: " + str(e)
        )

def execute_broadcast(admin_chat_id):
    with DB_LOCK:
        conn = db_connect()
        c = conn.cursor()
        c.execute("SELECT user_id FROM users")
        users = [r[0] for r in c.fetchall()]
        conn.close()

    txt_template = get_set("bcast_text_html") or get_set("bcast_text")
    m_id = get_set("bcast_media_id")
    m_type = get_set("bcast_media_type")

    markup = build_keyboard_from_string(
        get_set("bcast_buttons")
    )

    do_pin = get_set("bcast_pin")

    succ = 0
    fail = 0
    start_t = time.time()

    bot.send_message(
        admin_chat_id,
        "🚀 <b>प्रसारण शुरू...</b>\n"
        f"कुल यूज़र्स: {len(users)}"
    )

    for u in users:
        try:
            txt=_render_template(txt_template, get_main_user(u))
            if m_type == "photo" and m_id != "NONE":
                m = bot.send_photo(
                    u,
                    m_id,
                    caption=txt,
                    reply_markup=markup,
                    parse_mode="HTML"
                )

            elif m_type == "video" and m_id != "NONE":
                m = bot.send_video(
                    u,
                    m_id,
                    caption=txt,
                    reply_markup=markup,
                    parse_mode="HTML"
                )

            else:
                m = bot.send_message(
                    u,
                    txt,
                    reply_markup=markup,
                    parse_mode="HTML"
                )

            if do_pin == "YES":
                try:
                    bot.pin_chat_message(
                        u,
                        m.message_id
                    )
                except Exception:
                    pass

            with DB_LOCK:
                conn = db_connect()
                conn.execute(
                    "UPDATE users SET status='active' WHERE user_id=%s",
                    (u,)
                )
                conn.commit()
                conn.close()

            succ += 1

        except Exception:
            fail += 1

            with DB_LOCK:
                conn = db_connect()
                conn.execute(
                    "UPDATE users SET status='blocked' WHERE user_id=%s",
                    (u,)
                )
                conn.commit()
                conn.close()

        # Telegram flood-control safety.
        time.sleep(0.05)

    elapsed = round(
        time.time() - start_t,
        2
    )

    rep = (
        "📢 <b>प्रसारण पूरा!</b>\n\n"
        f"🟢 सफल: {succ}\n"
        f"🔴 विफल: {fail}\n"
        f"⏱️ समय: {elapsed} सेकंड"
    )

    bot.send_message(
        admin_chat_id,
        rep
    )

# ============================================================
# LIVE USER CHAT
# ============================================================
def check_user_msg(message):
    if message.chat.type != "private":
        return False

    if is_admin(message.from_user.id):
        return False

    if message.text and message.text.startswith("/"):
        return False

    return True

def send_temporary_success(chat_id):
    """Show a temporary success message and remove it after 3 seconds."""
    try:
        sent = bot.send_message(
            chat_id,
            "✉️ <b>MESSAGE SENT SUCCESSFULLY</b>"
        )

        def delete_after_3_seconds():
            try:
                bot.delete_message(chat_id, sent.message_id)
            except Exception:
                pass

        threading.Timer(3.0, delete_after_3_seconds).start()
    except Exception:
        pass


# In-memory fallback keeps live-chat replies working even if PostgreSQL
# has a temporary connection/write problem. Database mapping remains the
# persistent source whenever it is available.
LIVE_MAP_MEMORY = {}


def copy_user_message_to_admin(message):
    """Deliver a user message to admin and confirm success only after delivery."""
    # IMPORTANT: Only failure of the actual Telegram copy is a delivery failure.
    # Auxiliary operations (DB mapping, profile button, admin info) must never
    # make the user see a false "MESSAGE COULD NOT BE SENT" message.
    try:
        # Copy without reply_markup first. This is the most compatible
        # copy_message call across pyTelegramBotAPI / Bot API versions.
        admin_msg = bot.copy_message(
            ADMIN_ID,
            message.chat.id,
            message.message_id
        )
    except Exception as e:
        # The message was not copied to admin: this is a genuine delivery
        # failure, so show the failure message to the user.
        try:
            bot.send_message(
                message.chat.id,
                "❌ <b>MESSAGE COULD NOT BE SENT</b>\nPlease try again."
            )
        except Exception:
            pass

        try:
            bot.send_message(
                ADMIN_ID,
                "📩 <b>User message delivery failed.</b>\n"
                f"User ID: <code>{message.from_user.id}</code>\n"
                f"Error: <code>{str(e)[:500]}</code>"
            )
        except Exception:
            pass
        return

    # From this point the message is already in the admin chat.
    # Never turn later auxiliary errors into a false user-facing failure.
    LIVE_MAP_MEMORY[admin_msg.message_id] = message.from_user.id

    # Persistent reply mapping. If PostgreSQL is temporarily unavailable,
    # the in-memory mapping above still allows the admin to reply.
    try:
        with DB_LOCK:
            conn = db_connect()
            conn.execute(
                """
                INSERT INTO live_map
                (admin_message_id, user_id)
                VALUES (%s, %s)
                ON CONFLICT (admin_message_id) DO UPDATE
                SET user_id=EXCLUDED.user_id
                """,
                (admin_msg.message_id, message.from_user.id)
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print("Live-map DB save warning:", e)

    # Add the existing profile button after the copy succeeds. If this
    # optional UI operation fails, the delivered message is still valid.
    try:
        profile_markup = types.InlineKeyboardMarkup(row_width=1)
        profile_markup.add(
            types.InlineKeyboardButton(
                "👤 Open User Profile",
                url=f"tg://user?id={message.from_user.id}"
            )
        )
        bot.edit_message_reply_markup(
            ADMIN_ID,
            admin_msg.message_id,
            reply_markup=profile_markup
        )
    except Exception as e:
        print("Profile button warning:", e)

    # Admin info is auxiliary and must not affect delivery status.
    try:
        info = (
            f"👤 <b>User:</b> "
            f"{message.from_user.first_name or 'User'}\n"
            f"🆔 <code>{message.from_user.id}</code>"
        )
        bot.send_message(
            ADMIN_ID,
            info,
            reply_to_message_id=admin_msg.message_id
        )
    except Exception as e:
        print("Admin user-info warning:", e)

    # The actual Telegram copy succeeded, so show the temporary success message.
    send_temporary_success(message.chat.id)

@bot.message_handler(
    func=check_user_msg,
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "sticker",
        "voice",
        "audio",
        "animation",
        "contact",
        "location"
    ]
)
def user_live_chat(message):
    add_user(message.from_user.id, message.from_user)
    copy_user_message_to_admin(message)

# ============================================================
# ADMIN LIVE CHAT REPLY
# ============================================================
@bot.message_handler(
    func=lambda m: (
        m.chat.type == "private"
        and is_admin(m.from_user.id)
        and m.reply_to_message is not None
    ),
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "sticker",
        "voice",
        "audio",
        "animation",
        "contact",
        "location"
    ]
)
def admin_reply_to_user(message):
    replied_id = message.reply_to_message.message_id

    # First try connected-bot live chat mappings.
    try:
        mrow = mdb("SELECT bot_id,user_id FROM managed_live_map WHERE admin_message_id=%s", (replied_id,), one=True)
        if mrow:
            mbot = MANAGED_BOTS.get(int(mrow[0]))
            if mbot:
                mbot.copy_message(int(mrow[1]), ADMIN_ID, message.message_id)
                bot.send_message(ADMIN_ID, "✅ Reply connected bot user को भेज दिया गया।")
                return
    except Exception as e:
        print("Managed live-map warning:", e)

    # Persistent DB mapping is preferred. In-memory mapping is the fallback
    # for a temporary PostgreSQL issue after the message was already delivered.
    row = None
    try:
        with DB_LOCK:
            conn = db_connect()
            c = conn.cursor()
            c.execute(
                "SELECT user_id FROM live_map WHERE admin_message_id=%s",
                (replied_id,)
            )
            row = c.fetchone()
            conn.close()
    except Exception as e:
        print("Live-map DB read warning:", e)

    user_id = row[0] if row else LIVE_MAP_MEMORY.get(replied_id)

    if not user_id:
        bot.send_message(
            ADMIN_ID,
            "❌ इस reply का user mapping नहीं मिला।"
        )
        return

    try:
        bot.copy_message(
            user_id,
            ADMIN_ID,
            message.message_id
        )

        bot.send_message(
            ADMIN_ID,
            "✅ Reply user को भेज दिया गया।"
        )

    except Exception as e:
        bot.send_message(
            ADMIN_ID,
            "❌ User को reply नहीं भेज सका:\n" +
            str(e)
        )

# ============================================================
# ERROR / POLLING
# ============================================================
def start_saved_managed_bots():
    rows=mdb("SELECT id,bot_token,status FROM managed_bots WHERE status IN ('active','error') AND bot_token IS NOT NULL AND bot_token <> ''",all_rows=True) or []
    for bid,token,status in rows:
        threading.Thread(target=run_managed_bot,args=(bid,token),daemon=True).start()
        time.sleep(0.2)

def run_bot():
    print("========================================")
    print("Telegram bot is starting...")
    print("========================================")

    while True:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )

        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)

if __name__ == "__main__":
    start_saved_managed_bots()
    run_bot()
