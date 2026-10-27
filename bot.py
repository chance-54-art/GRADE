"""GRADE - mini app Telegram de minage (monnaie GRAD).

Minage une fois par 24 h, decide et verifie cote serveur.
Un seul processus: bot Telegram (webhook), serveur HTTP aiohttp qui sert
index.html et l'API /api/<action>, stockage SQLite.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("minex")

# ---------------------------------------------------------------------------
# Configuration (variables d'environnement Render)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
PORT = int(os.environ.get("PORT", "10000"))
# Render fournit RENDER_EXTERNAL_URL automatiquement (https://xxx.onrender.com)
BASE_URL = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("BASE_URL") or "").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "minex.db")
# Optionnel: canal officiel pour la tache "Rejoindre le canal".
# CHANNEL_ID = @nom_du_canal (le bot doit etre admin du canal)
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "").strip()

INDEX_PATH = Path(__file__).resolve().parent / "index.html"
APP_NAME = "GRADE"
CURRENCY = "GRAD"
# Langues de l'interface (10 plus utilisees), stockees dans users.lang
LANG_CODES = ("fr", "en", "zh", "hi", "es", "ar", "bn", "pt", "ru", "ur", "id", "de", "ja", "sw", "mr", "te", "tr", "ta", "vi", "ko", "it", "th", "gu", "fa", "pl", "uk", "ro", "nl", "el", "hu", "cs", "sv", "da", "fi", "no", "he", "ms", "tl", "am", "ha", "yo", "ig", "zu", "xh", "af", "so", "rw", "ln", "kg", "lu", "ny", "sn", "om", "ti", "lg", "wo", "ff", "mg", "ku", "ps", "az", "uz", "kk", "ky", "tg", "tk", "hy", "ka", "mn", "ne", "si", "my", "km", "lo", "sr", "hr", "bs", "sk", "sl", "lt", "lv", "et", "is", "ga", "ca", "gl", "eu", "mt", "sq", "mk", "bg", "be", "lb", "fo", "ceb", "jv", "su", "ml", "kn", "pa")

MINE_REWARD = 240             # GRAD par cycle de 24 h (= 10 GRAD par heure de base)
# Daily Rewards: jour 1 = 100 GRAD, +50 par jour, jour 7 = 400 GRAD, puis le cycle reprend
DAILY_BASE = 100
DAILY_STEP = 50
DAILY_DAYS = 7
DAILY_COOLDOWN = 86400
MINE_COOLDOWN = 86400         # un minage toutes les 24 h
REFERRAL_BONUS = 500          # GRAD pour le parrain a chaque ami inscrit
REFERRED_WELCOME = 250        # GRAD offerts a l'ami invite
WELCOME_BONUS = 1000         # GRAD credites une seule fois a la creation du compte (flag welcomed_at)
INITDATA_MAX_AGE = 86400

TASKS = [
    {"id": "join_channel", "title": "Rejoindre le canal officiel", "reward": 1000, "kind": "channel"},
    {"id": "mines_7", "title": "Faire 7 minages", "reward": 700, "kind": "mines", "goal": 7},
    {"id": "invite_3", "title": "Inviter 3 amis", "reward": 1500, "kind": "invites", "goal": 3},
    {"id": "wallet", "title": "Ajouter ton portefeuille", "reward": 200, "kind": "wallet"},
]
TASK_BY_ID = {t["id"]: t for t in TASKS}

# Ligues (palier = GRAD total du solde)
LEAGUES = [(0, "Bronze"), (1000, "Argent"), (5000, "Or"), (20000, "Platine"), (100000, "Diamant")]
# Ameliorations: +10 % du taux par niveau, prix x1.5 a chaque niveau
UPGRADES = [
    {"id": "gpu", "title": "GPU", "base": 200},
    {"id": "ferme", "title": "Ferme", "base": 300},
    {"id": "noeud", "title": "Noeud", "base": 450},
    {"id": "serveur", "title": "Serveur", "base": 650},
    {"id": "pool", "title": "Pool", "base": 900},
    {"id": "usine", "title": "Usine", "base": 1200},
]
UPGRADE_BY_ID = {u["id"]: u for u in UPGRADES}
UPGRADE_STEP = 0.10
UPGRADE_COST_MULT = 1.5
COMBO_SIZE = 3
COMBO_BONUS = 500
CIPHER_BONUS = 250
CIPHER_WORDS = ["GRAD", "MINE", "LUNE", "ETOILE", "BLOC", "CRYPTO", "BOURSE", "ROCHE"]
MORSE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..",
}
STREAK_STEP = 10             # bonus de connexion: 10 GRAD x jours consecutifs
STREAK_CAP = 150
FRIEND_TIERS = [(5, 2000), (10, 5000), (25, 15000)]
DAY_S = 86400

WEBHOOK_SECRET = hashlib.sha256(("minex-webhook:" + BOT_TOKEN).encode()).hexdigest()[:48]
BOT_USERNAME = ""  # rempli au demarrage via get_me()

# ---------------------------------------------------------------------------
# Base de donnees SQLite
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    balance INTEGER NOT NULL DEFAULT 0,
    mines_count INTEGER NOT NULL DEFAULT 0,
    last_mine_at REAL,
    wallet TEXT,
    referred_by INTEGER,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_tasks (
    user_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (user_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC);
CREATE INDEX IF NOT EXISTS idx_users_ref ON users(referred_by);
CREATE TABLE IF NOT EXISTS user_upgrades (
    user_id INTEGER NOT NULL,
    upgrade_id TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, upgrade_id)
);
CREATE TABLE IF NOT EXISTS user_daily_claims (
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day INTEGER NOT NULL,
    PRIMARY KEY (user_id, kind, day)
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes des reglages si la base existe deja (version precedente)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "display_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
    if "notifications" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN notifications INTEGER NOT NULL DEFAULT 1")
    if "lang" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'fr'")
    if "mine_credited" not in cols:
        # GRAD du cycle deja verse au solde. Les anciens minages (version instantanee)
        # etaient entierement credites: on marque 1000 pour ne rien recréditer.
        conn.execute("ALTER TABLE users ADD COLUMN mine_credited INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET mine_credited = ? WHERE last_mine_at IS NOT NULL", (MINE_REWARD,))
    if "welcomed_at" not in cols:
        # Bonus de bienvenue: NULL = pas encore credite. Les comptes existants (crees avant)
        # sont credites au premier /api/me via db_claim_welcome (solde + flag dans un seul UPDATE).
        conn.execute("ALTER TABLE users ADD COLUMN welcomed_at REAL")
    if "daily_last_claim" not in cols:
        # Calendrier Daily Rewards : dernier claim (epoch) et jour courant (1 a 7)
        conn.execute("ALTER TABLE users ADD COLUMN daily_last_claim REAL NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN daily_day INTEGER NOT NULL DEFAULT 0")
    if "streak_days" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN streak_days INTEGER NOT NULL DEFAULT 0")
    if "streak_last_day" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN streak_last_day INTEGER NOT NULL DEFAULT -1")
    if "cycle_mult" not in cols:
        # multiplicateur de taux fige au debut du cycle en cours
        conn.execute("ALTER TABLE users ADD COLUMN cycle_mult REAL NOT NULL DEFAULT 1.0")
    conn.commit()


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


DB = open_db()


def db_get_user(uid: int):
    return DB.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def cycle_accrued(last_mine_at, now: float, mult: float = 1.0) -> int:
    """GRAD du cycle en cours: floor(MINE_REWARD * mult * temps ecoule / 24 h), plafonne a MINE_REWARD * mult."""
    if last_mine_at is None:
        return 0
    elapsed = min(max(now - last_mine_at, 0.0), MINE_COOLDOWN)
    return math.floor(MINE_REWARD * (mult or 1.0) * elapsed / MINE_COOLDOWN + 1e-9)


def settle_user(uid: int) -> None:
    """Verse au solde le GRAD accumule depuis la derniere lecture (sans jamais crediter deux fois)."""
    now = time.time()
    row = DB.execute("SELECT last_mine_at, mine_credited, cycle_mult FROM users WHERE id = ?", (uid,)).fetchone()
    if not row or row["last_mine_at"] is None:
        return
    acc = cycle_accrued(row["last_mine_at"], now, row["cycle_mult"])
    if acc <= row["mine_credited"]:
        return
    # CAS: ne met a jour que si personne n'a deja credite cette tranche
    DB.execute(
        "UPDATE users SET balance = balance + ?, mine_credited = ? WHERE id = ? AND mine_credited = ?",
        (acc - row["mine_credited"], acc, uid, row["mine_credited"]),
    )
    DB.commit()


def settle_all() -> None:
    """Met a jour tous les cycles en cours (utile avant le classement)."""
    rows = DB.execute("SELECT id FROM users WHERE last_mine_at IS NOT NULL").fetchall()
    for r in rows:
        settle_user(r["id"])


def db_ensure_user(uid: int, username, first_name, ref_id=None):
    """Cree l'utilisateur si besoin (avec parrain), sinon met a jour son nom."""
    row = db_get_user(uid)
    if row:
        if row["username"] != username or row["first_name"] != first_name:
            DB.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE id = ?",
                (username, first_name, uid),
            )
            DB.commit()
        return row, False

    referred_by = None
    if ref_id and ref_id != uid and db_get_user(ref_id):
        referred_by = ref_id

    now = time.time()
    # Bonus de bienvenue credite dans le meme INSERT que la creation (flag welcomed_at pose
    # en meme temps: pas de double credit possible).
    start_balance = (REFERRED_WELCOME if referred_by else 0) + WELCOME_BONUS
    DB.execute(
        "INSERT INTO users (id, username, first_name, balance, referred_by, created, welcomed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uid, username, first_name, start_balance, referred_by, now, now),
    )
    if referred_by:
        DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (REFERRAL_BONUS, referred_by))
    DB.commit()
    return db_get_user(uid), True


def db_claim_welcome(uid: int) -> bool:
    """Credite WELCOME_BONUS une seule fois. Solde et flag sont poses dans UN seul UPDATE
    (atomique): si le flag est deja pose, la condition WHERE echoue et rien n'est credite."""
    cur = DB.execute(
        "UPDATE users SET balance = balance + ?, welcomed_at = ? WHERE id = ? AND welcomed_at IS NULL",
        (WELCOME_BONUS, time.time(), uid),
    )
    DB.commit()
    return cur.rowcount == 1


def db_count_referrals(uid: int) -> int:
    return DB.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (uid,)).fetchone()[0]


def db_rank(balance: int) -> int:
    return DB.execute("SELECT COUNT(*) FROM users WHERE balance > ?", (balance,)).fetchone()[0] + 1


def db_claimed_tasks(uid: int) -> set:
    rows = DB.execute("SELECT task_id FROM user_tasks WHERE user_id = ?", (uid,)).fetchall()
    return {r["task_id"] for r in rows}


# ---------------------------------------------------------------------------
# Regles
# ---------------------------------------------------------------------------
def display_name_of(row) -> str:
    """Pseudo affiche: celui choisi dans les reglages, sinon le prenom Telegram."""
    return (row["display_name"] or row["first_name"] or "Mineur").strip() or "Mineur"


def fmt_duration(seconds: float) -> str:
    """Format 'XXm YYs' (ou 'Xh YYm' au-dela d'une heure)."""
    total = max(0, int(seconds))
    h, rest = divmod(total, 3600)
    m, sec = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m:02d}m {sec:02d}s"


def task_status(task: dict, row, refs: int):
    """Retourne (terminee, texte de progression)."""
    kind = task["kind"]
    if kind == "mines":
        count = row["mines_count"]
        return count >= task["goal"], f"{min(count, task['goal'])}/{task['goal']}"
    if kind == "invites":
        return refs >= task["goal"], f"{min(refs, task['goal'])}/{task['goal']}"
    if kind == "wallet":
        return bool(row["wallet"]), "Portefeuille ajouté" if row["wallet"] else "Pas encore ajouté"
    return False, "Vérification au moment de réclamer"


def daily_bonus(day: int) -> int:
    return DAILY_BASE + DAILY_STEP * (day - 1)


def daily_next_day(row, now: float) -> int:
    """Jour qui serait credite au prochain claim (meme regle que act_daily_claim)."""
    last = float(row["daily_last_claim"] or 0)
    if not last or (now - last) >= 2 * DAILY_COOLDOWN:
        return 1
    return (int(row["daily_day"] or 0) % DAILY_DAYS) + 1


def daily_state(row, now: float) -> dict:
    """Etat du calendrier Daily Rewards, derive des valeurs serveur."""
    last = float(row["daily_last_claim"] or 0)
    can = (not last) or (now - last) >= DAILY_COOLDOWN
    nxt = daily_next_day(row, now)
    return {
        "can_claim": can,
        "day": int(row["daily_day"] or 0),
        "next_day": nxt,
        "next_bonus": daily_bonus(nxt),
        "next_claim_at": 0 if can else int(last + DAILY_COOLDOWN),
        "bonuses": [daily_bonus(d) for d in range(1, DAILY_DAYS + 1)],
        "days": DAILY_DAYS,
    }


# ---------------------------------------------------------------------------
# GRADE+ : ligues, ameliorations, combo, cipher, streak, paliers d'amis
# Tout est calcule et verifie cote serveur. Le client n'affiche que ces valeurs.
# ---------------------------------------------------------------------------
def day_index(now: float) -> int:
    """Jour courant (UTC), sert de seed deterministe cote serveur."""
    return int(now // DAY_S)


def league_of(balance: int) -> dict:
    idx = 0
    for i, (threshold, _) in enumerate(LEAGUES):
        if balance >= threshold:
            idx = i
    name = LEAGUES[idx][1]
    nxt = LEAGUES[idx + 1] if idx + 1 < len(LEAGUES) else None
    return {
        "index": idx,
        "name": name,
        "next_name": nxt[1] if nxt else "",
        "next_at": nxt[0] if nxt else 0,
        "floor": LEAGUES[idx][0],
    }


def upgrade_levels(uid: int) -> dict:
    rows = DB.execute("SELECT upgrade_id, level FROM user_upgrades WHERE user_id = ?", (uid,)).fetchall()
    return {r["upgrade_id"]: r["level"] for r in rows}


def upgrade_mult(uid: int) -> float:
    """Multiplicateur du taux: +10 % par niveau cumule sur toutes les ameliorations."""
    total = sum(upgrade_levels(uid).values())
    return round(1.0 + UPGRADE_STEP * total, 4)


def upgrade_price(base: int, level: int) -> int:
    return int(math.floor(base * (UPGRADE_COST_MULT ** level)))


def combo_for_day(day: int) -> list:
    import random
    return random.Random(f"combo-{day}").sample([u["id"] for u in UPGRADES], COMBO_SIZE)


def cipher_for_day(day: int) -> str:
    import random
    return random.Random(f"cipher-{day}").choice(CIPHER_WORDS)


def morse_encode(word: str) -> str:
    return " / ".join(" ".join(MORSE[c] for c in ch) for ch in word)


def claimed_today(uid: int, kind: str, day: int) -> bool:
    return DB.execute(
        "SELECT 1 FROM user_daily_claims WHERE user_id = ? AND kind = ? AND day = ?", (uid, kind, day)
    ).fetchone() is not None


def bought_today(uid: int, day: int) -> set:
    rows = DB.execute(
        "SELECT kind FROM user_daily_claims WHERE user_id = ? AND day = ? AND kind LIKE 'buy:%'", (uid, day)
    ).fetchall()
    return {r["kind"][4:] for r in rows}


def streak_bonus(days: int) -> int:
    return min(STREAK_STEP * days, STREAK_CAP)


def update_streak(uid: int, now: float) -> int:
    """Met a jour la serie de connexions (1 fois par jour) et credite le bonus du jour. Retourne le gain."""
    today = day_index(now)
    row = db_get_user(uid)
    last = int(row["streak_last_day"] if row["streak_last_day"] is not None else -1)
    if last == today:
        return 0
    days = (int(row["streak_days"] or 0) + 1) if last == today - 1 else 1
    bonus = streak_bonus(days)
    cur = DB.execute(
        "UPDATE users SET streak_days = ?, streak_last_day = ?, balance = balance + ?"
        " WHERE id = ? AND (streak_last_day IS NULL OR streak_last_day < ?)",
        (days, today, bonus, uid, today),
    )
    DB.commit()
    return bonus if cur.rowcount == 1 else 0


def extras_state(uid: int, row, refs: int, claimed: set, now: float) -> dict:
    today = day_index(now)
    levels = upgrade_levels(uid)
    bal = row["balance"]
    lg = league_of(bal)
    ups = []
    for u in UPGRADES:
        lvl = levels.get(u["id"], 0)
        ups.append({
            "id": u["id"], "title": u["title"], "level": lvl,
            "price": upgrade_price(u["base"], lvl),
            "affordable": bal >= upgrade_price(u["base"], lvl),
        })
    combo_ids = combo_for_day(today)
    bought = bought_today(uid, today)
    combo_done_by_user = claimed_today(uid, "combo", today)
    combo_ready = all(i in bought for i in combo_ids)
    names = {u["id"]: u["title"] for u in UPGRADES}
    cipher_word_done = claimed_today(uid, "cipher", today)
    tiers = []
    for goal, reward in FRIEND_TIERS:
        tid = f"friend_{goal}"
        tiers.append({
            "id": tid, "goal": goal, "reward": reward,
            "progress": min(refs, goal),
            "done": refs >= goal, "claimed": tid in claimed,
        })
    streak = int(row["streak_days"] or 0)
    return {
        "league": lg,
        "upgrades": ups,
        "mult": upgrade_mult(uid),
        "hourly_base": round(MINE_REWARD / 24, 2),
        "hourly_rate": round(MINE_REWARD / 24 * upgrade_mult(uid), 2),
        "combo": {
            "ids": combo_ids,
            "titles": [names[i] for i in combo_ids],
            "bought": [i in bought for i in combo_ids],
            "ready": combo_ready,
            "claimed": combo_done_by_user,
            "bonus": COMBO_BONUS,
        },
        "cipher": {
            "morse": morse_encode(cipher_for_day(today)),
            "claimed": cipher_word_done,
            "bonus": CIPHER_BONUS,
        },
        "streak": {
            "days": streak,
            "next_bonus": streak_bonus(streak + 1 if int(row["streak_last_day"] or -1) != today else streak),
            "today_done": int(row["streak_last_day"] or -1) == today,
        },
        "friend_tiers": tiers,
    }


async def act_upgrade_buy(uid, body, request):
    """Achete un niveau d'amelioration. Prix verifie et debite dans une seule requete SQL."""
    uid_up = str(body.get("upgrade_id", ""))
    up = UPGRADE_BY_ID.get(uid_up)
    if not up:
        raise ApiError("Amélioration inconnue.")
    settle_user(uid)
    now = time.time()
    level = upgrade_levels(uid).get(uid_up, 0)
    price = upgrade_price(up["base"], level)
    cur = DB.execute(
        "UPDATE users SET balance = balance - ? WHERE id = ? AND balance >= ?", (price, uid, price)
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError(f"Pas assez de GRAD : il faut {price} {CURRENCY}.")
    DB.execute(
        "INSERT INTO user_upgrades (user_id, upgrade_id, level) VALUES (?, ?, 1)"
        " ON CONFLICT(user_id, upgrade_id) DO UPDATE SET level = level + 1",
        (uid, uid_up),
    )
    DB.execute(
        "INSERT OR IGNORE INTO user_daily_claims (user_id, kind, day) VALUES (?, ?, ?)",
        (uid, "buy:" + uid_up, day_index(now)),
    )
    DB.commit()
    return {"ok": True, "message": f"{up['title']} niveau {level + 1} acheté pour {price} {CURRENCY}. "
            "Le taux augmente au prochain minage.", **build_state(uid)}


async def act_combo_claim(uid, body, request):
    """Combo du jour: les 3 cartes achetees le meme jour, +500 GRAD, une seule fois par jour."""
    now = time.time()
    today = day_index(now)
    if claimed_today(uid, "combo", today):
        raise ApiError("Combo déjà réclamé aujourd'hui.")
    ids = combo_for_day(today)
    bought = bought_today(uid, today)
    if not all(i in bought for i in ids):
        raise ApiError("Achète les 3 cartes du combo aujourd'hui pour le réclamer.")
    settle_user(uid)
    cur = DB.execute(
        "INSERT OR IGNORE INTO user_daily_claims (user_id, kind, day) VALUES (?, 'combo', ?)", (uid, today)
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError("Combo déjà réclamé aujourd'hui.")
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (COMBO_BONUS, uid))
    DB.commit()
    return {"ok": True, "gained": COMBO_BONUS, "message": f"Combo réussi : +{COMBO_BONUS} {CURRENCY} !",
            **build_state(uid)}


async def act_cipher_claim(uid, body, request):
    """Cipher du jour: decoder le morse, +250 GRAD, une fois par jour, verifie cote serveur."""
    now = time.time()
    today = day_index(now)
    if claimed_today(uid, "cipher", today):
        raise ApiError("Cipher déjà réussi aujourd'hui.")
    answer = str(body.get("answer", "")).strip().upper()
    if answer != cipher_for_day(today):
        raise ApiError("Mauvais code. Essaie encore.")
    settle_user(uid)
    cur = DB.execute(
        "INSERT OR IGNORE INTO user_daily_claims (user_id, kind, day) VALUES (?, 'cipher', ?)", (uid, today)
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError("Cipher déjà réussi aujourd'hui.")
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (CIPHER_BONUS, uid))
    DB.commit()
    return {"ok": True, "gained": CIPHER_BONUS, "message": f"Cipher décodé : +{CIPHER_BONUS} {CURRENCY} !",
            **build_state(uid)}


async def act_friend_tier_claim(uid, body, request):
    """Palier d'amis: 5 amis = +2000, etc. Reclame une seule fois, verifie le nombre d'amis cote serveur."""
    tid = str(body.get("tier_id", ""))
    tier = next((t for t in FRIEND_TIERS if f"friend_{t[0]}" == tid), None)
    if not tier:
        raise ApiError("Palier inconnu.")
    goal, reward = tier
    if db_count_referrals(uid) < goal:
        raise ApiError(f"Il te faut {goal} amis invités pour ce palier.")
    settle_user(uid)
    cur = DB.execute(
        "INSERT OR IGNORE INTO user_tasks (user_id, task_id, claimed_at) VALUES (?, ?, ?)",
        (uid, tid, time.time()),
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError("Palier déjà réclamé.")
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (reward, uid))
    DB.commit()
    return {"ok": True, "gained": reward, "message": f"Palier {goal} amis : +{reward} {CURRENCY} !",
            **build_state(uid)}


def build_state(uid: int) -> dict:
    db_claim_welcome(uid)  # comptes existants: credite une fois (no-op si deja fait)
    settle_user(uid)
    row = db_get_user(uid)
    now = time.time()
    refs = db_count_referrals(uid)
    claimed = db_claimed_tasks(uid)
    balance = row["balance"]

    # 0 = jamais mine (minage possible tout de suite)
    next_mine_at = (row["last_mine_at"] + MINE_COOLDOWN) if row["last_mine_at"] else 0
    can_mine = now >= next_mine_at
    # GRAD du cycle deja credite au solde, et GRAD encore en cours d'accumulation
    cycle_credited = int(row["mine_credited"] or 0) if row["last_mine_at"] else 0
    cycle_pending = cycle_accrued(row["last_mine_at"], now, row["cycle_mult"]) if row["last_mine_at"] else 0

    me = {
        "id": uid,
        "name": display_name_of(row),
        "display_name": row["display_name"] or "",
        "notifications": bool(row["notifications"]),
        "lang": row["lang"] if row["lang"] in LANG_CODES else "fr",
        "username": row["username"] or "",
        "balance": balance,
        "mines_count": row["mines_count"],
        "wallet": row["wallet"] or "",
        "referrals": refs,
        "referral_earned": refs * REFERRAL_BONUS,
        "referral_bonus": REFERRAL_BONUS,
        "referred_welcome": REFERRED_WELCOME,
        "rank": db_rank(balance),
        "created": row["created"],
        "ref_link": f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else "",
        "currency": CURRENCY,
        "app_name": APP_NAME,
        "mine_reward": MINE_REWARD,
        "mine_cooldown_s": MINE_COOLDOWN,
        "can_mine": can_mine,
        "cycle_credited": cycle_credited,
        "cycle_accrued": cycle_pending,
        "cycle_start": float(row["last_mine_at"] or 0),
        "next_mine_at": int(next_mine_at),
        "seconds_left": 0 if can_mine else int(next_mine_at - now) + 1,
        "server_now": now,
        "daily": daily_state(row, now),
    }

    tasks = []
    for t in TASKS:
        done, progress = task_status(t, row, refs)
        available = True
        if t["kind"] == "channel":
            available = bool(CHANNEL_ID)
            progress = "Vérification au moment de réclamer" if available else "Bientôt disponible"
        tasks.append({
            "id": t["id"],
            "title": t["title"],
            "reward": t["reward"],
            "claimed": t["id"] in claimed,
            "done": done,
            "progress": progress,
            "available": available,
            "link": CHANNEL_LINK if t["kind"] == "channel" else "",
        })

    extra = extras_state(uid, row, refs, claimed, now)
    me.update(extra)
    return {"me": me, "tasks": tasks}


# ---------------------------------------------------------------------------
# Verification de la session Telegram Mini App (initData)
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str):
    if not init_data:
        return None
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc_hash, received_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        return None
    if time.time() - auth_date > INITDATA_MAX_AGE:
        return None
    try:
        user = json.loads(parsed.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if "id" in user else None


# ---------------------------------------------------------------------------
# Actions de l'API (appelees par index.html via POST /api/<action>)
# ---------------------------------------------------------------------------
class ApiError(Exception):
    pass


async def act_me(uid, body, request):
    gain = update_streak(uid, time.time())
    if gain:
        return {"ok": True, "gained": gain, "message": f"Connexion 🔥 : +{gain} {CURRENCY}",
                **build_state(uid)}
    return {"ok": True, **build_state(uid)}


async def act_mine(uid, body, request):
    """Demarre un cycle de minage (une fois par 24 h). Le GRAD du cycle s'accumule
    ensuite en continu: floor(MINE_REWARD * temps ecoule / 24 h), verse au solde a chaque lecture.
    La verification et le demarrage sont une seule requete SQL (pas de course entre deux clics)."""
    settle_user(uid)  # solde le cycle precedent (plein) avant d'en commencer un nouveau
    now = time.time()
    cur = DB.execute(
        "UPDATE users SET mines_count = mines_count + 1, last_mine_at = ?, mine_credited = 0, cycle_mult = ?"
        " WHERE id = ? AND (last_mine_at IS NULL OR last_mine_at <= ?)",
        (now, upgrade_mult(uid), uid, now - MINE_COOLDOWN),
    )
    DB.commit()
    if cur.rowcount != 1:
        row = db_get_user(uid)
        left = (row["last_mine_at"] + MINE_COOLDOWN) - now
        raise ApiError(f"Minage pas encore disponible. Prochain minage dans {fmt_duration(left)}.")
    return {"ok": True, "gained": 0,
            "message": f"Minage lancé : +{MINE_REWARD} {CURRENCY} sur 24 h.", **build_state(uid)}


async def act_task_claim(uid, body, request):
    task_id = str(body.get("task_id", ""))
    task = TASK_BY_ID.get(task_id)
    if not task:
        raise ApiError("Tâche inconnue.")
    if task_id in db_claimed_tasks(uid):
        raise ApiError("Tu as déjà réclamé cette tâche.")

    if task["kind"] == "channel":
        if not CHANNEL_ID:
            raise ApiError("Tâche indisponible pour le moment.")
        tg = request.app.get("tg")
        if tg is None:
            # Bot Telegram pas encore pret : on ne peut pas verifier le canal, on le dit proprement
            raise ApiError("Vérification du canal indisponible pour le moment. Réessaie plus tard.")
        try:
            member = await tg.bot.get_chat_member(CHANNEL_ID, uid)
        except Exception:
            log.exception("get_chat_member a échoué pour %s", uid)
            raise ApiError("Impossible de vérifier le canal pour le moment. Réessaie plus tard.")
        if member.status not in ("member", "administrator", "creator"):
            raise ApiError("Tu n'es pas encore membre du canal.")
    else:
        row = db_get_user(uid)
        done, _ = task_status(task, row, db_count_referrals(uid))
        if not done:
            raise ApiError("Tâche pas encore terminée.")

    cur = DB.execute(
        "INSERT OR IGNORE INTO user_tasks (user_id, task_id, claimed_at) VALUES (?, ?, ?)",
        (uid, task_id, time.time()),
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError("Tu as déjà réclamé cette tâche.")
    DB.execute("UPDATE users SET balance = balance + ? WHERE id = ?", (task["reward"], uid))
    DB.commit()
    return {"ok": True, "gained": task["reward"],
            "message": f"+{task['reward']} {CURRENCY} : {task['title']}", **build_state(uid)}


async def act_daily_claim(uid, body, request):
    """Claim du jour: 1 fois par 24 h, verifie cote serveur, anti-double-claim atomique."""
    now = time.time()
    row = db_get_user(uid)
    last = float(row["daily_last_claim"] or 0)
    if last and (now - last) < DAILY_COOLDOWN:
        raise ApiError("Tu as déjà réclamé ton bonus aujourd'hui. Reviens dans 24 h.")
    # Serie: si le dernier claim date de plus de 48 h, on repart au jour 1
    prev_day = int(row["daily_day"] or 0)
    if not last or (now - last) >= 2 * DAILY_COOLDOWN:
        day = 1
    else:
        day = (prev_day % DAILY_DAYS) + 1
    bonus = daily_bonus(day)
    # Claim atomique: la condition est re-verifiee dans l'UPDATE (pas de double credit en parallele)
    cur = DB.execute(
        "UPDATE users SET daily_last_claim = ?, daily_day = ?, balance = balance + ? "
        "WHERE id = ? AND daily_last_claim <= ?",
        (now, day, bonus, uid, now - DAILY_COOLDOWN),
    )
    if cur.rowcount != 1:
        DB.rollback()
        raise ApiError("Tu as déjà réclamé ton bonus aujourd'hui. Reviens dans 24 h.")
    DB.commit()
    return {"ok": True, "gained": bonus, "day": day,
            "message": f"Jour {day} : +{bonus} {CURRENCY}", **build_state(uid)}


async def act_leaderboard(uid, body, request):
    settle_all()
    rows = DB.execute(
        "SELECT * FROM users ORDER BY balance DESC, id ASC LIMIT 10"
    ).fetchall()
    top = [
        {"rank": i + 1, "name": display_name_of(r), "balance": r["balance"], "me": r["id"] == uid}
        for i, r in enumerate(rows)
    ]
    me_row = db_get_user(uid)
    return {"ok": True, "top": top, "my_rank": db_rank(me_row["balance"]), "my_balance": me_row["balance"]}


async def act_referrals(uid, body, request):
    rows = DB.execute(
        "SELECT * FROM users WHERE referred_by = ? ORDER BY created DESC LIMIT 100",
        (uid,),
    ).fetchall()
    count = db_count_referrals(uid)
    return {
        "ok": True,
        "count": count,
        "earned": count * REFERRAL_BONUS,
        "bonus": REFERRAL_BONUS,
        "welcome": REFERRED_WELCOME,
        "link": f"https://t.me/{BOT_USERNAME}?start=ref_{uid}" if BOT_USERNAME else "",
        "list": [{"name": display_name_of(r), "joined": r["created"]} for r in rows],
    }


async def act_wallet_set(uid, body, request):
    address = str(body.get("address", "")).strip()
    if not (8 <= len(address) <= 120) or any(c.isspace() for c in address):
        raise ApiError("Adresse invalide (8 à 120 caractères, sans espaces).")
    DB.execute("UPDATE users SET wallet = ? WHERE id = ?", (address, uid))
    DB.commit()
    return {"ok": True, "message": "Portefeuille enregistré.", **build_state(uid)}


async def act_settings_name(uid, body, request):
    """Pseudo affiche (1 a 24 caracteres). Vide = revenir au prenom Telegram."""
    raw = str(body.get("name", "")).strip()
    if any(ord(c) < 32 for c in raw):
        raise ApiError("Pseudo invalide.")
    if len(raw) > 24:
        raise ApiError("Le pseudo ne doit pas dépasser 24 caractères.")
    DB.execute("UPDATE users SET display_name = ? WHERE id = ?", (raw or None, uid))
    DB.commit()
    return {"ok": True, "message": "Pseudo enregistré." if raw else "Pseudo remis au prénom Telegram.",
            **build_state(uid)}


async def act_settings_notifications(uid, body, request):
    enabled = 1 if body.get("enabled") in (True, 1, "1", "true") else 0
    DB.execute("UPDATE users SET notifications = ? WHERE id = ?", (enabled, uid))
    DB.commit()
    return {"ok": True,
            "message": "Notifications activées." if enabled else "Notifications désactivées.",
            **build_state(uid)}


async def act_settings_lang(uid, body, request):
    """Langue de l'interface (une des 10 langues), stockee en SQLite."""
    code = str(body.get("lang", "")).strip().lower()
    if code not in LANG_CODES:
        raise ApiError("Langue non prise en charge.")
    DB.execute("UPDATE users SET lang = ? WHERE id = ?", (code, uid))
    DB.commit()
    return {"ok": True, **build_state(uid)}


async def act_settings_reset(uid, body, request):
    """Remet solde, minages et tâches reclamees a zero. Amis, portefeuille et pseudo sont conserves."""
    if str(body.get("confirm", "")).strip().upper() != "RESET":
        raise ApiError("Confirmation manquante : tape RESET pour réinitialiser.")
    DB.execute("UPDATE users SET balance = 0, mines_count = 0, last_mine_at = NULL, mine_credited = 0 WHERE id = ?", (uid,))
    DB.execute("DELETE FROM user_tasks WHERE user_id = ?", (uid,))
    DB.commit()
    return {"ok": True, "message": "Compte réinitialisé : solde et minages remis à zéro.", **build_state(uid)}


ACTIONS = {
    "me": act_me,
    "mine": act_mine,
    "task_claim": act_task_claim,
    "daily_claim": act_daily_claim,
    "leaderboard": act_leaderboard,
    "referrals": act_referrals,
    "wallet_set": act_wallet_set,
    "settings_name": act_settings_name,
    "settings_notifications": act_settings_notifications,
    "settings_lang": act_settings_lang,
    "settings_reset": act_settings_reset,
    "upgrade_buy": act_upgrade_buy,
    "combo_claim": act_combo_claim,
    "cipher_claim": act_cipher_claim,
    "friend_tier_claim": act_friend_tier_claim,
}


async def api_route(request: web.Request) -> web.Response:
    action = request.match_info["action"]
    fn = ACTIONS.get(action)
    if fn is None:
        return web.json_response({"ok": False, "error": "Action inconnue."}, status=404)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    tg_user = verify_init_data(str(body.get("initData", "")))
    if not tg_user:
        return web.json_response(
            {"ok": False, "error": "Session invalide. Ouvre GRADE depuis Telegram."}, status=401
        )

    uid = int(tg_user["id"])
    db_ensure_user(uid, tg_user.get("username"), tg_user.get("first_name"))
    try:
        data = await fn(uid, body, request)
    except ApiError as e:
        data = {"ok": False, "error": str(e), **build_state(uid)}
    except Exception:
        log.exception("Erreur dans l'action %s", action)
        data = {"ok": False, "error": "Erreur serveur, réessaie dans un instant.", **build_state(uid)}
    return web.json_response(data)


# ---------------------------------------------------------------------------
# Webhook Telegram + pages statiques
# ---------------------------------------------------------------------------
async def telegram_webhook(request: web.Request) -> web.Response:
    token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(token, WEBHOOK_SECRET):
        return web.Response(status=403)
    tg = request.app["tg"]
    try:
        data = await request.json()
        update = Update.de_json(data, tg.bot)
        await tg.process_update(update)
    except Exception:
        log.exception("Erreur de traitement d'une mise a jour Telegram")
    return web.Response(text="ok")


async def index(request: web.Request) -> web.Response:
    if not INDEX_PATH.exists():
        return web.Response(text="index.html introuvable", status=500)
    return web.FileResponse(INDEX_PATH, headers={"Cache-Control": "no-cache"})


async def coin_image(request: web.Request) -> web.Response:
    path = Path(__file__).resolve().parent / "grad-coin.jpg"
    if not path.exists():
        return web.Response(text="image introuvable", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# ---------------------------------------------------------------------------
# Commandes du bot
# ---------------------------------------------------------------------------
def webapp_keyboard() -> InlineKeyboardMarkup | None:
    if not BASE_URL:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⛏ Ouvrir GRADE", web_app=WebAppInfo(url=BASE_URL + "/"))]]
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    ref_id = None
    if context.args:
        arg = context.args[0]
        # Lien de parrainage: ?start=ref_ID
        if arg.startswith("ref_") and arg[4:].isdigit():
            ref_id = int(arg[4:])

    _, created = db_ensure_user(user.id, user.username, user.first_name, ref_id)

    lines = [
        "⛏ Bienvenue sur GRADE !",
        "",
        f"Appuie sur Miner : tu mines {MINE_REWARD // 24} {CURRENCY} par heure ({MINE_REWARD} {CURRENCY} par cycle de 24 h). "
        "Invite tes amis et gagne encore plus.",
    ]
    if created:
        lines += ["", f"🎁 Bonus de bienvenue : +{WELCOME_BONUS} {CURRENCY} crédités."]
    if created and ref_id and db_get_user(ref_id):
        lines += ["", f"🎁 Tu as rejoint via un parrain : +{REFERRED_WELCOME} {CURRENCY} offerts."]
    lines += ["", "Appuie sur le bouton ci-dessous pour lancer l'app."]

    kb = webapp_keyboard()
    if kb is None:
        lines += ["", "(L'adresse de l'app n'est pas encore configurée, réessaie plus tard.)"]
    await update.message.reply_text("\n".join(lines), reply_markup=kb)


async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    row, _ = db_ensure_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        f"💰 Ton solde : {row['balance']} {CURRENCY}\n"
        f"Tu as {db_count_referrals(user.id)} ami(s) parrainé(s)."
    )


async def cmd_aide(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commandes :\n"
        "/start : ouvrir GRADE\n"
        "/solde : voir ton solde\n"
        "/aide : cette aide\n\n"
        f"Tu mines {MINE_REWARD // 24} {CURRENCY} par heure ({MINE_REWARD} {CURRENCY} par cycle de 24 h). "
        "Parraine tes amis avec ton lien depuis l'onglet Amis."
    )


# ---------------------------------------------------------------------------
# Demarrage
# ---------------------------------------------------------------------------
async def main() -> None:
    global BOT_USERNAME

    tg = Application.builder().token(BOT_TOKEN).updater(None).build()
    tg.add_handler(CommandHandler("start", cmd_start))
    tg.add_handler(CommandHandler("solde", cmd_solde))
    tg.add_handler(CommandHandler("aide", cmd_aide))

    await tg.initialize()
    await tg.start()
    me = await tg.bot.get_me()
    BOT_USERNAME = me.username or ""
    log.info("Bot connecte : @%s", BOT_USERNAME)

    if BASE_URL:
        await tg.bot.set_webhook(
            url=BASE_URL + "/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        await tg.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="GRADE", web_app=WebAppInfo(url=BASE_URL + "/"))
        )
        log.info("Webhook configure sur %s/webhook", BASE_URL)
    else:
        log.warning("Ni RENDER_EXTERNAL_URL ni BASE_URL: webhook et WebApp non configures.")

    web_app = web.Application()
    web_app["tg"] = tg
    web_app.router.add_get("/", index)
    web_app.router.add_get("/health", health)
    web_app.router.add_get("/grad-coin.jpg", coin_image)
    web_app.router.add_post("/webhook", telegram_webhook)
    web_app.router.add_post("/api/{action}", api_route)

    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Serveur HTTP sur le port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await tg.stop()
        await tg.shutdown()
        DB.close()


if __name__ == "__main__":
    asyncio.run(main())
