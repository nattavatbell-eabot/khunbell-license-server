import os
import sqlite3
import threading
from datetime import datetime, timezone

from flask import Flask, request, jsonify
import discord
from discord.ext import commands


# =========================
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
EA_API_KEY = os.getenv("EA_API_KEY", "CHANGE_THIS_SECRET")

PORT = int(os.getenv("PORT", "10000"))

DB_FILE = "licenses.db"


# =========================
# DATABASE
# =========================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                account_id TEXT PRIMARY KEY,
                broker TEXT,
                ea_name TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT,
                updated_at TEXT
            )
        """)

        conn.commit()
        conn.close()


def get_license(account_id):
    with db_lock:
        conn = get_db()

        row = conn.execute(
            "SELECT * FROM licenses WHERE account_id = ?",
            (str(account_id),)
        ).fetchone()

        conn.close()
        return row


def create_pending(account_id, broker, ea_name):
    now = datetime.now(timezone.utc).isoformat()

    with db_lock:
        conn = get_db()

        conn.execute("""
            INSERT INTO licenses
            (account_id, broker, ea_name, status, created_at, updated_at)
            VALUES (?, ?, ?, 'PENDING', ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                broker = excluded.broker,
                ea_name = excluded.ea_name,
                updated_at = excluded.updated_at
        """, (
            str(account_id),
            broker,
            ea_name,
            now,
            now
        ))

        conn.commit()
        conn.close()


def set_status(account_id, status):
    now = datetime.now(timezone.utc).isoformat()

    with db_lock:
        conn = get_db()

        conn.execute("""
            UPDATE licenses
            SET status = ?, updated_at = ?
            WHERE account_id = ?
        """, (
            status,
            now,
            str(account_id)
        ))

        conn.commit()
        conn.close()


# =========================
# FLASK API
# =========================

app = Flask(__name__)


def check_api_key():
    key = request.headers.get("X-License-Key", "")

    if not EA_API_KEY or key != EA_API_KEY:
        return False

    return True


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "Khunbell EA License Server",
        "status": "online"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok"
    })


# =========================
# EA REQUEST LICENSE
# =========================

@app.route("/request", methods=["POST"])
def request_license():

    if not check_api_key():
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    data = request.get_json(silent=True) or {}

    account_id = str(data.get("account_id", "")).strip()
    broker = str(data.get("broker", "")).strip()
    ea_name = str(data.get("ea_name", "Khunbell EA")).strip()

    if not account_id:
        return jsonify({
            "ok": False,
            "error": "account_id required"
        }), 400

    existing = get_license(account_id)

    if existing:
        status = existing["status"]

        return jsonify({
            "ok": True,
            "account_id": account_id,
            "status": status
        })

    create_pending(
        account_id,
        broker,
        ea_name
    )

    # ส่ง Discord แจ้งเตือน
    try:
        bot.loop.call_soon_threadsafe(
            lambda: bot.loop.create_task(
                send_license_request(
                    account_id,
                    broker,
                    ea_name
                )
            )
        )
    except Exception as e:
        print("Discord notification error:", e)

    return jsonify({
        "ok": True,
        "account_id": account_id,
        "status": "PENDING"
    })


# =========================
# EA CHECK LICENSE
# =========================

@app.route("/check", methods=["GET"])
def check_license():

    if not check_api_key():
        return jsonify({
            "ok": False,
            "error": "Unauthorized"
        }), 401

    account_id = request.args.get("account_id", "").strip()

    if not account_id:
        return jsonify({
            "ok": False,
            "error": "account_id required"
        }), 400

    row = get_license(account_id)

    if not row:
        return jsonify({
            "ok": True,
            "account_id": account_id,
            "status": "NOT_FOUND"
        })

    return jsonify({
        "ok": True,
        "account_id": account_id,
        "status": row["status"]
    })


# =========================
# DISCORD BOT
# =========================

intents = discord.Intents.none()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# =========================
# APPROVE / BLOCK BUTTON
# =========================

class LicenseView(discord.ui.View):

    def __init__(self, account_id=None):
        super().__init__(timeout=None)

        if account_id:
            approve_id = f"approve:{account_id}"
            block_id = f"block:{account_id}"

            self.add_item(
                discord.ui.Button(
                    label="APPROVE",
                    style=discord.ButtonStyle.success,
                    custom_id=approve_id
                )
            )

            self.add_item(
                discord.ui.Button(
                    label="BLOCK",
                    style=discord.ButtonStyle.danger,
                    custom_id=block_id
                )


# =========================
# BUTTON HANDLER
# =========================

@bot.event
async def on_interaction(interaction: discord.Interaction):

    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")

    if custom_id.startswith("approve:"):

        account_id = custom_id.split(":", 1)[1]

        row = get_license(account_id)

        if not row:
            await interaction.response.send_message(
                f"❌ ไม่พบ Account `{account_id}`",
                ephemeral=True
            )
            return

        set_status(account_id, "ACTIVE")

        await interaction.response.edit_message(
            content=(
                f"🔔 **KHUNBELL EA LICENSE**\n\n"
                f"Account: `{account_id}`\n"
                f"Broker: `{row['broker']}`\n"
                f"EA: `{row['ea_name']}`\n\n"
                f"สถานะ: 🟢 **ACTIVE / APPROVED**"
            ),
            view=None
        )

        return

    if custom_id.startswith("block:"):

        account_id = custom_id.split(":", 1)[1]

        row = get_license(account_id)

        if not row:
            await interaction.response.send_message(
                f"❌ ไม่พบ Account `{account_id}`",
                ephemeral=True
            )
            return

        set_status(account_id, "BLOCKED")

        await interaction.response.edit_message(
            content=(
                f"🔔 **KHUNBELL EA LICENSE**\n\n"
                f"Account: `{account_id}`\n"
                f"Broker: `{row['broker']}`\n"
                f"EA: `{row['ea_name']}`\n\n"
                f"สถานะ: 🔴 **BLOCKED**"
            ),
            view=None
        )

        return


# =========================
# SEND DISCORD REQUEST
# =========================

async def send_license_request(account_id, broker, ea_name):

    for guild in bot.guilds:

        for channel in guild.text_channels:

            try:

                if not channel.permissions_for(guild.me).send_messages:
                    continue

                embed = discord.Embed(
                    title="🔔 NEW LICENSE REQUEST",
                    description=(
                        f"**Account:** `{account_id}`\n"
                        f"**Broker:** `{broker}`\n"
                        f"**EA:** `{ea_name}`\n\n"
                        f"**Status:** 🟡 PENDING"
                    ),
                    color=0xF1C40F
                )

                view = LicenseView(account_id)

                await channel.send(
                    embed=embed,
                    view=view
                )

                return

            except Exception as e:
                print("Discord send error:", e)

    print("No writable Discord channel found.")


# =========================
# BOT START
# =========================

@bot.event
async def on_ready():

    print(f"Discord Bot logged in as {bot.user}")

    # Persistent views
    with db_lock:
        conn = get_db()

        rows = conn.execute("""
            SELECT account_id
            FROM licenses
            WHERE status = 'PENDING'
        """).fetchall()

        conn.close()

    for row in rows:
        try:
            bot.add_view(
                LicenseView(row["account_id"])
            )
        except Exception as e:
            print("View error:", e)


# =========================
# RUN
# =========================

init_db()


def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT
    )


if __name__ == "__main__":

    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    flask_thread.start()

    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN is not configured.")
        raise SystemExit(1)

    bot.run(DISCORD_TOKEN)
