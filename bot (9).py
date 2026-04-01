"""
╔══════════════════════════════════════════════════════════╗
║           Illumix Core VPS Bot  —  Production            ║
║  Python 3.12+ | discord.py 2.x | Docker VPS Management  ║
╚══════════════════════════════════════════════════════════╝

All commands use wizard menus (dropdowns + buttons + embeds).
Sensitive info (SSH creds, Pinggy URLs) → DM only.
Public embeds show progress/status with no credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import secrets
import string
import subprocess
import time
from datetime import datetime, timedelta, timezone

import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════
TOKEN               = os.getenv("BOT")
if not TOKEN:
    raise RuntimeError("❌  BOT token missing in .env")

GUILD_ID            = 1426562614422671382
MAIN_ADMIN_IDS: set[int] = {1212951893651759225}
OWNER_ID            = 1212951893651759225
SERVER_IP           = "127.0.0.1"
IMAGE_UBUNTU        = "jrei/systemd-ubuntu:22.04"
IMAGE_DEBIAN        = "jrei/systemd-debian:12"
DEFAULT_RAM_GB      = 8
DEFAULT_CPU         = 2
DEFAULT_DISK_GB     = 20
POINTS_PER_DEPLOY   = 40
POINTS_RENEW_15     = 10
POINTS_RENEW_30     = 20
VPS_LIFETIME_DAYS   = 15
LOG_CHANNEL_ID: int | None = None
ALLOWED_CHANNELS    = [1470834030902513664]
ADMIN_BYPASS_CHANNELS = True
BOT_START_TIME      = time.time()

DATA_DIR        = "data"
USERS_FILE      = os.path.join(DATA_DIR, "users.json")
VPS_FILE        = os.path.join(DATA_DIR, "vps_db.json")
INV_CACHE_FILE  = os.path.join(DATA_DIR, "inv_cache.json")
GIVEAWAY_FILE   = os.path.join(DATA_DIR, "giveaways.json")
RENEW_MODE_FILE = os.path.join(DATA_DIR, "renew_mode.json")

OS_IMAGES = {"ubuntu": IMAGE_UBUNTU, "debian": IMAGE_DEBIAN}
ADMIN_IDS: set[int] = set(MAIN_ADMIN_IDS) | {OWNER_ID}

os.makedirs(DATA_DIR, exist_ok=True)

# ════════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════════
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IllumixCore")

# ════════════════════════════════════════════════════════════
#  COLORS  — Illumix Core palette
# ════════════════════════════════════════════════════════════
class C:
    SUCCESS  = discord.Color.from_str("#00E676")   # vivid green
    ERROR    = discord.Color.from_str("#FF1744")   # vivid red
    WARNING  = discord.Color.from_str("#FF9100")   # amber
    INFO     = discord.Color.from_str("#448AFF")   # indigo-blue
    PREMIUM  = discord.Color.from_str("#FFD740")   # gold
    ADMIN    = discord.Color.from_str("#EA80FC")   # purple
    VPS      = discord.Color.from_str("#18FFFF")   # cyan
    GIVEAWAY = discord.Color.from_str("#FF4081")   # pink
    GAME     = discord.Color.from_str("#69F0AE")   # mint
    DARK     = discord.Color.from_str("#212121")   # near-black

# ════════════════════════════════════════════════════════════
#  JSON HELPERS  (atomic writes, safe reads)
# ════════════════════════════════════════════════════════════
def load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path) as f:
            data = json.load(f)
        if isinstance(default, dict) and not isinstance(data, dict):
            return {}
        if isinstance(default, list)  and not isinstance(data, list):
            return []
        return data
    except Exception:
        return default


def save_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ── in-memory stores ──────────────────────────────────────
users:           dict = load_json(USERS_FILE,      {})
vps_db:          dict = load_json(VPS_FILE,        {})
invite_snapshot: dict = load_json(INV_CACHE_FILE,  {})
giveaways:       dict = load_json(GIVEAWAY_FILE,   {})
renew_mode:      dict = load_json(RENEW_MODE_FILE, {"mode": "15"})


def persist_users():     save_json(USERS_FILE,      users)
def persist_vps():       save_json(VPS_FILE,        vps_db)
def persist_giveaways(): save_json(GIVEAWAY_FILE,   giveaways)
def persist_renew():     save_json(RENEW_MODE_FILE, renew_mode)

# ── active tunnel registry  (in-memory, rebuilt on restart) ──
# { "cid:port": { "proto", "port", "public_url", "pid", "created_at" } }
active_tunnels: dict = {}

# ── game state (in-memory per session) ──
# rps_streaks[uid]  = int   (consecutive wins)
# game_cooldowns[uid] = float (unix timestamp of last play)
rps_streaks:    dict[int, int]   = {}
game_cooldowns: dict[int, float] = {}
GAME_COOLDOWN_SECS = 15      # seconds between games
RPS_WIN_PTS   = 3
RPS_STREAK_BONUS = 2         # extra pts every 3-win streak
NUM_GUESS_PTS = 5
LUCK_COOLDOWN_SECS = 30

# ════════════════════════════════════════════════════════════
#  CREDENTIAL GENERATOR
# ════════════════════════════════════════════════════════════
_ALPHA = string.ascii_lowercase
_ALNUM = string.ascii_letters + string.digits


def _gen_username() -> str:
    """illumix_<6 random lowercase letters>"""
    suffix = "".join(secrets.choice(_ALPHA) for _ in range(6))
    return f"illumix_{suffix}"


def _gen_password(length: int = 16) -> str:
    """Random 16-char alphanumeric + symbols password."""
    symbols = "!@#$%^&*"
    alphabet = _ALNUM + symbols
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        # Ensure at least one digit, one upper, one symbol
        if (any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in symbols for c in pwd)):
            return pwd


def _credentials_taken(username: str) -> bool:
    return any(v.get("vps_user") == username for v in vps_db.values())


def generate_unique_credentials() -> tuple[str, str]:
    """Return (username, password) guaranteed unique across all VPS records."""
    for _ in range(20):
        username = _gen_username()
        if not _credentials_taken(username):
            return username, _gen_password()
    # Fallback: UUID-based
    import uuid
    return f"illumix_{uuid.uuid4().hex[:8]}", _gen_password()

# ════════════════════════════════════════════════════════════
#  EMBED BUILDER
# ════════════════════════════════════════════════════════════
FOOTER_TEXT = "Illumix Core"
FOOTER_ICON = None   # set to a URL string to add an icon


def _embed(
    title:       str,
    description: str = "",
    color=C.INFO,
    thumbnail:   str | None = None,
    image:       str | None = None,
) -> discord.Embed:
    e = discord.Embed(title=title, description=description,
                      color=color, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_ICON)
    if thumbnail:
        e.set_thumbnail(url=thumbnail)
    if image:
        e.set_image(url=image)
    return e


def e_ok(desc: str,  title: str = "✅  Success")  -> discord.Embed: return _embed(title, desc, C.SUCCESS)
def e_err(desc: str, title: str = "❌  Error")    -> discord.Embed: return _embed(title, desc, C.ERROR)
def e_warn(desc: str,title: str = "⚠️  Warning") -> discord.Embed: return _embed(title, desc, C.WARNING)
def e_info(desc: str,title: str = "ℹ️  Info")    -> discord.Embed: return _embed(title, desc, C.INFO)

# ════════════════════════════════════════════════════════════
#  PERMISSION HELPERS
# ════════════════════════════════════════════════════════════
async def check_channel(interaction: discord.Interaction) -> bool:
    if not ALLOWED_CHANNELS:
        return True
    if ADMIN_BYPASS_CHANNELS and interaction.user.id in ADMIN_IDS:
        return True
    if interaction.channel_id in ALLOWED_CHANNELS:
        return True
    mentions = []
    for ch_id in ALLOWED_CHANNELS:
        ch = interaction.guild.get_channel(ch_id)
        if ch:
            mentions.append(ch.mention)
    chs = ", ".join(mentions) or "designated channels"
    await interaction.response.send_message(
        embed=e_err(f"Use this command in: {chs}"), ephemeral=True
    )
    return False


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def get_user_vps(uid: int) -> list[dict]:
    s = str(uid)
    return [v for v in vps_db.values()
            if v["owner"] == s or s in v.get("shared_with", [])]


def owns_vps(uid: int, cid: str) -> bool:
    """Strict ownership (or admin bypass)."""
    if uid in ADMIN_IDS:
        return True
    v = vps_db.get(cid)
    return v is not None and v["owner"] == str(uid)


def can_manage_vps(uid: int, cid: str) -> bool:
    """Owner OR shared user (or admin)."""
    if uid in ADMIN_IDS:
        return True
    v = vps_db.get(cid)
    if not v:
        return False
    s = str(uid)
    return v["owner"] == s or s in v.get("shared_with", [])

# ════════════════════════════════════════════════════════════
#  DOCKER HELPERS
# ════════════════════════════════════════════════════════════
async def _run(*cmd: str, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, out.decode().strip(), err.decode().strip()
    except asyncio.TimeoutError:
        return -1, "", "Timeout"
    except Exception as exc:
        return -1, "", str(exc)


async def docker_run_container(
    ram_gb: int, cpu: int, disk_gb: int, os_choice: str = "ubuntu"
) -> tuple[str | None, int | None, str | None]:
    port  = random.randint(3000, 3999)
    name  = f"vps-{secrets.token_hex(4)}"
    image = OS_IMAGES.get(os_choice.lower(), IMAGE_UBUNTU)
    code, out, err = await _run(
        "docker", "run", "-d",
        "--privileged", "--cgroupns=host",
        "--tmpfs", "/run", "--tmpfs", "/run/lock",
        "-v", "/sys/fs/cgroup:/sys/fs/cgroup:rw",
        "--name", name,
        "--cpus", str(cpu),
        "--memory", f"{ram_gb}g",
        "--memory-swap", f"{ram_gb}g",
        "-p", f"{port}:80",
        image,
        timeout=60,
    )
    if code != 0:
        return None, None, err or "Unknown docker error"
    cid = out[:12]
    return cid or None, port, None


async def docker_create_user(cid: str, username: str, password: str) -> bool:
    """Create a Linux user inside the container with the given credentials."""
    cmds = [
        f"useradd -m -s /bin/bash {username}",
        f"echo '{username}:{password}' | chpasswd",
        f"usermod -aG sudo {username}",
    ]
    for cmd in cmds:
        code, _, _ = await _run("docker", "exec", cid, "bash", "-c", cmd, timeout=30)
        if code != 0:
            logger.warning(f"User creation cmd failed ({cmd}): {cid}")
    return True


async def _setup_container_ssh(cid: str, password: str) -> None:
    """Auto-configure SSH root access inside the container after creation."""
    ssh_cmds = [
        "apt-get install -y openssh-server openssh-client",
        "mkdir -p /var/run/sshd",
        f"echo 'root:{password}' | chpasswd",
        "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config",
        "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config",
        "pkill sshd >/dev/null 2>&1 || true",
        "/usr/sbin/sshd",
    ]
    for cmd in ssh_cmds:
        code, _, err = await _run("docker", "exec", cid, "bash", "-c", cmd, timeout=60)
        if code != 0:
            logger.warning(f"SSH setup cmd failed [{cmd}] on {cid}: {err}")
    # Verify sshd is running
    _, verify_out, _ = await _run(
        "docker", "exec", cid, "bash", "-c", "ps aux | grep sshd", timeout=10
    )
    logger.info(f"SSH setup verify [{cid}]: {verify_out[:200]}")


async def setup_vps_environment(cid: str) -> bool:
    await asyncio.sleep(15)
    pkgs = "tmate curl wget neofetch sudo nano htop openssh-server"
    for cmd in [
        "apt-get update -y",
        f"apt-get install -y {pkgs}",
        "systemctl enable ssh || service ssh start || true",
        "systemctl enable systemd-user-sessions || true",
    ]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "exec", cid, "bash", "-c", cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            logger.warning(f"Timeout: {cmd}")
        except Exception as exc:
            logger.warning(f"Cmd error: {exc}")
    code, _, _ = await _run("docker", "exec", cid, "systemctl", "--version", timeout=10)
    return code == 0


async def docker_exec_capture_ssh(cid: str) -> str:
    kill_cmd = "pkill -f tmate || true"
    await _run("docker", "exec", cid, "bash", "-c", kill_cmd, timeout=10)
    sock = f"/tmp/tmate-{cid}.sock"
    ssh_cmd = (
        f"tmate -S {sock} new-session -d && "
        f"sleep 5 && "
        f"tmate -S {sock} display -p '#{{tmate_ssh}}'"
    )
    _, out, _ = await _run("docker", "exec", cid, "bash", "-c", ssh_cmd, timeout=30)
    return out or "ssh@tmate.io"


async def docker_stop(cid: str)    -> bool: rc, _, _ = await _run("docker", "stop",    cid); return rc == 0
async def docker_start(cid: str)   -> bool: rc, _, _ = await _run("docker", "start",   cid); return rc == 0
async def docker_restart(cid: str) -> bool: rc, _, _ = await _run("docker", "restart", cid); return rc == 0
async def docker_remove(cid: str)  -> bool: rc, _, _ = await _run("docker", "rm", "-f", cid); return rc == 0


async def docker_running(cid: str) -> bool:
    _, out, _ = await _run(
        "docker", "inspect", "--format", "{{.State.Running}}", cid, timeout=10
    )
    return out.lower() == "true"


async def docker_stats(cid: str) -> dict | None:
    _, out, _ = await _run(
        "docker", "stats", cid, "--no-stream", "--format",
        "{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}|{{.BlockIO}}",
        timeout=15,
    )
    if out:
        p = out.split("|")
        if len(p) >= 5:
            return {"cpu": p[0], "mem_usage": p[1], "mem_perc": p[2],
                    "net_io": p[3], "block_io": p[4]}
    return None


def sys_stats() -> dict | None:
    try:
        cpu  = psutil.cpu_percent(interval=0.5)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu":      cpu,
            "ram_pct":  mem.percent,
            "ram_used": round(mem.used  / 1024**3, 1),
            "ram_tot":  round(mem.total / 1024**3, 1),
            "disk_pct": disk.percent,
            "disk_used":round(disk.used  / 1024**3, 1),
            "disk_tot": round(disk.total / 1024**3, 1),
        }
    except Exception:
        return None

# ════════════════════════════════════════════════════════════
#  VPS FACTORY
# ════════════════════════════════════════════════════════════
async def create_vps(
    owner_id:    int,
    ram:         int = DEFAULT_RAM_GB,
    cpu:         int = DEFAULT_CPU,
    disk:        int = DEFAULT_DISK_GB,
    paid:        bool = False,
    giveaway:    bool = False,
    expire_days: int  = VPS_LIFETIME_DAYS,
    os_choice:   str  = "ubuntu",
) -> dict:
    cid, http_port, err = await docker_run_container(ram, cpu, disk, os_choice)
    if err:
        return {"error": err}

    await asyncio.sleep(8)
    systemctl_ok = await setup_vps_environment(cid)

    # Create unique OS user
    username, password = generate_unique_credentials()
    await docker_create_user(cid, username, password)

    # Auto-configure SSH root access on every newly created container
    await _setup_container_ssh(cid, password)

    ssh_line = await docker_exec_capture_ssh(cid)

    now     = datetime.now(timezone.utc)
    expires = now + timedelta(days=expire_days)
    rec = {
        "owner":             str(owner_id),
        "container_id":      cid,
        "os":                os_choice.lower(),
        "ram":               ram,
        "cpu":               cpu,
        "disk":              disk,
        "http_port":         http_port,
        "ssh":               ssh_line,
        "vps_user":          username,
        "vps_pass":          password,
        "created_at":        now.isoformat(),
        "expires_at":        expires.isoformat(),
        "active":            True,
        "suspended":         False,
        "paid_plan":         paid,
        "giveaway_vps":      giveaway,
        "shared_with":       [],
        "additional_ports":  [],
        "systemctl_working": systemctl_ok,
    }
    vps_db[cid] = rec
    persist_vps()
    return rec

# ════════════════════════════════════════════════════════════
#  LOGGING HELPER
# ════════════════════════════════════════════════════════════
async def send_log(action: str, user, details: str = "", vps_id: str = ""):
    if not LOG_CHANNEL_ID:
        return
    try:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if not ch:
            return
        e = _embed(f"📋  {action}", color=C.ADMIN, )
        if hasattr(user, "mention"):
            e.add_field(name="👤 User", value=f"{user.mention} `{user.name}`", inline=True)
        else:
            e.add_field(name="👤 User", value=f"`{user}`", inline=True)
        if vps_id:
            e.add_field(name="🆔 VPS",    value=f"`{vps_id}`", inline=True)
        if details:
            e.add_field(name="📝 Details", value=details[:1024], inline=False)
        e.add_field(name="⏰ Time", value=f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>", inline=True)
        await ch.send(embed=e)
        logs_file = os.path.join(DATA_DIR, "vps_logs.json")
        logs      = load_json(logs_file, [])
        logs.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "user":   getattr(user, "name", str(user)),
            "details": details,
            "vps_id": vps_id,
        })
        if len(logs) > 1000:
            logs = logs[-1000:]
        save_json(logs_file, logs)
    except Exception as exc:
        logger.error(f"send_log: {exc}")

# ════════════════════════════════════════════════════════════
#  BOT INIT
# ════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
intents.guilds  = True
intents.members = True
intents.invites = True


class IllumixBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} commands")
        except Exception as exc:
            logger.error(f"Sync failed: {exc}")


bot = IllumixBot()

# ════════════════════════════════════════════════════════════
#  ─────────────────── WIZARD COMPONENTS ────────────────────
# ════════════════════════════════════════════════════════════

# ── Reusable: owned-VPS dropdown ────────────────────────────
class OwnedVPSSelect(discord.ui.Select):
    """Populates with the calling user's VPS (strict owner only)."""

    def __init__(self, uid: int, on_select, placeholder: str = "Select your VPS…"):
        self._on_select = on_select
        vps_list = [v for v in vps_db.values() if v["owner"] == str(uid)]
        options  = []
        for v in vps_list[:25]:
            st = "🟢" if v["active"] and not v.get("suspended") else (
                 "⏸️" if v.get("suspended") else "🔴")
            oi = "🟠" if v.get("os", "ubuntu") == "ubuntu" else "🔵"
            options.append(discord.SelectOption(
                label=f"{st} {oi} {v['container_id']}",
                value=v["container_id"],
                description=f"{v['ram']}GB RAM · {v['cpu']} CPU · exp {v['expires_at'][:10]}",
            ))
        if not options:
            options = [discord.SelectOption(
                label="No VPS found", value="__none__",
                description="Deploy one with /deploy",
            )]
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message(
                embed=e_err("You have no VPS. Use `/deploy` to create one."), ephemeral=True
            )
            return
        await self._on_select(interaction, self.values[0])


# ── Reusable: all-VPS dropdown (admin) ──────────────────────
class AllVPSSelect(discord.ui.Select):
    """Populates with every VPS in the database (admin use). Supports optional status filter."""

    def __init__(self, on_select, placeholder: str = "Select a VPS…",
                 filter_fn=None):
        self._on_select = on_select
        vps_list = [v for v in vps_db.values()]
        if filter_fn:
            vps_list = [v for v in vps_list if filter_fn(v)]
        options = []
        for v in vps_list[:25]:
            st = "🟢" if v["active"] and not v.get("suspended") else (
                 "⏸️" if v.get("suspended") else "🔴")
            oi = "🟠" if v.get("os", "ubuntu") == "ubuntu" else "🔵"
            options.append(discord.SelectOption(
                label=f"{st} {oi} {v['container_id']}",
                value=v["container_id"],
                description=f"Owner: {v['owner']} · {v['ram']}GB · {v['cpu']}CPU · exp {v['expires_at'][:10]}",
            ))
        if not options:
            options = [discord.SelectOption(
                label="No VPS found", value="__none__",
                description="Nothing matches this filter",
            )]
        super().__init__(placeholder=placeholder, options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message(
                embed=e_err("No VPS available for this action."), ephemeral=True
            )
            return
        await self._on_select(interaction, self.values[0])


# ── Admin VPS Action View ─────────────────────────────
class VPSActionView(discord.ui.View):
    """View for admin to pick a VPS from dropdown and perform actions (delete, suspend)."""

    def __init__(self):
        super().__init__(timeout=60)
        # Add VPS dropdown
        self.add_item(AllVPSSelect(self._vps_picked))

    async def _vps_picked(self, interaction: discord.Interaction, container_id: str):
        """Callback when admin selects a VPS from the dropdown."""
        if container_id == "__none__":
            await interaction.response.send_message(
                embed=e_err("No VPS available for this action."), ephemeral=True
            )
            return

        # Clear previous items and add action buttons
        self.clear_items()

        # Delete Button
        delete_btn = discord.ui.Button(
            label="🗑️ Confirm Delete", style=discord.ButtonStyle.danger
        )

        async def do_delete_cb(interaction: discord.Interaction):
            try:
                await docker_stop(container_id)
                vps_db[container_id]["active"] = False
                persist_vps()
                await interaction.response.send_message(
                    f"✅ VPS `{container_id}` deleted!", ephemeral=True
                )
            except Exception as exc:
                logger.error(f"Error deleting VPS {container_id}: {exc}")
                await interaction.response.send_message(
                    f"❌ Failed to delete VPS `{container_id}`", ephemeral=True
                )

        delete_btn.callback = do_delete_cb
        self.add_item(delete_btn)

        # Suspend Button
        suspend_btn = discord.ui.Button(
            label="⏸️ Confirm Suspend", style=discord.ButtonStyle.danger
        )

        async def do_suspend_cb(interaction: discord.Interaction):
            try:
                await docker_suspend(container_id)
                vps_db[container_id]["suspended"] = True
                persist_vps()
                await interaction.response.send_message(
                    f"⏸️ VPS `{container_id}` suspended!", ephemeral=True
                )
            except Exception as exc:
                logger.error(f"Error suspending VPS {container_id}: {exc}")
                await interaction.response.send_message(
                    f"❌ Failed to suspend VPS `{container_id}`", ephemeral=True
                )

        suspend_btn.callback = do_suspend_cb
        self.add_item(suspend_btn)

        # Send the updated view to admin
        await interaction.response.send_message(
            embed=_embed(f"Selected VPS `{container_id}` — choose an action:", color=C.ADMIN),
            view=self, ephemeral=True
        )
# ════════════════════════════════════════════════════════════
#  /help  ─  public, dropdown categories + nav buttons
# ════════════════════════════════════════════════════════════
HELP_PAGES: dict[str, dict] = {
    "home": {
        "title": "🏠  Illumix Core — Help Center",
        "color": C.INFO,
        "fields": [
            ("👋  Welcome",
             "Illumix Core is a full-featured VPS management bot powered by Docker.\n\n"
             "Use the **dropdown** below to explore all command categories.", False),
            ("⚡  Quick Commands",
             "`/deploy` — Launch a VPS\n"
             "`/list` — Your VPS list\n"
             "`/manage` — Control panel\n"
             "`/port` — Port forwarding\n"
             "`/ping` — Bot latency\n"
             "`/game` — Game Center 🎮\n"
             "`/coinflip` — Bet points 🪙\n"
             "`/luck` — Mystery box 🍀", False),
        ],
    },
    "vps": {
        "title": "🖥️  VPS Management",
        "color": C.VPS,
        "fields": [
            ("Core", "`/deploy` — Deploy a VPS (costs points)\n"
                     "`/list` — List your VPS\n"
                     "`/manage` — Wizard control panel\n"
                     "`/remove` — Remove VPS (50% refund)", False),
            ("Sharing", "`/share_vps` — Grant access to a user\n"
                        "`/share_remove` — Revoke access", False),
            ("Cost", "Deploy: **40 pts** | Renew 15d: **10 pts** | Renew 30d: **20 pts**\n"
                     "VPS auto-suspends on expiry — renew to reactivate.", False),
        ],
    },
    "networking": {
        "title": "🌐  Networking & Port Forwarding",
        "color": C.PREMIUM,
        "fields": [
            ("Port Forwarding",
             "`/port` — Wizard to forward a port from your VPS\n\n"
             "Supports **TCP · UDP · HTTP**\n"
             "The bot runs the tunnel **inside your container** automatically.\n"
             "Public URL sent via DM only.", False),
            ("How It Works",
             "1. Run `/port` and select your VPS\n"
             "2. Choose **TCP / UDP / HTTP**\n"
             "3. Enter the internal port number\n"
             "4. Bot launches tunnel inside your container\n"
             "5. Receive the live public URL in your DMs", False),
        ],
    },
    "monitoring": {
        "title": "📊  Monitoring & Status",
        "color": C.VPS,
        "fields": [
            ("Commands",
             "`/status` — System health overview (auto-refreshes)\n"
             "`/ping` — Bot latency & uptime\n"
             "`/manage` → Monitor button — Per-VPS live stats", False),
            ("Health Indicators",
             "🟢 **Good** — All systems normal\n"
             "🟡 **Warning** — Elevated load\n"
             "🔴 **Critical** — Action required", False),
        ],
    },
    "admin": {
        "title": "🛡️  Admin Commands",
        "color": C.ADMIN,
        "fields": [
            ("VPS",
             "`/admin_create_vps` — Wizard VPS creation for any user\n"
             "`/listsall` — List all VPS\n"
             "`/suspend` / `/unsuspend`\n"
             "`/mass_port` — Bulk port add", False),
            ("Economy",
             "`/pointgive` / `/pointremove` / `/pointlistall`", False),
            ("System",
             "`/admin_add` / `/admin_remove` / `/admins`\n"
             "`/set_log_channel` / `/logs`\n"
             "`/giveaway_create` / `/giveaway_list`", False),
        ],
    },
    "points": {
        "title": "💰  Points & Invites",
        "color": C.GIVEAWAY,
        "fields": [
            ("Earn & Spend",
             "`/inv` — Invite stats\n"
             "`/claimpoint` — Convert invites → points\n"
             "`/pointbal` — Balance\n"
             "`/point_share` — Send points\n"
             "`/pointtop` — Leaderboard", False),
            ("How It Works",
             "1. Invite new members to the server\n"
             "2. Each unique join = 1 invite credit\n"
             "3. `/claimpoint` converts to spendable points\n"
             "4. Use points to `/deploy` and renew VPS", False),
        ],
    },
}


def _build_help_page(key: str, user: discord.User) -> discord.Embed:
    p = HELP_PAGES.get(key, HELP_PAGES["home"])
    e = _embed(p["title"], color=p["color"])
    for name, val, inline in p["fields"]:
        e.add_field(name=name, value=val, inline=inline)
    e.set_footer(text=f"Illumix Core  •  Requested by {user.name}")
    return e


class HelpCatSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="📚  Select a category…",
            options=[
                discord.SelectOption(label="🏠  Home",         value="home"),
                discord.SelectOption(label="🖥️  VPS",          value="vps"),
                discord.SelectOption(label="🌐  Networking",   value="networking"),
                discord.SelectOption(label="📊  Monitoring",   value="monitoring"),
                discord.SelectOption(label="🛡️  Admin",        value="admin"),
                discord.SelectOption(label="💰  Points",       value="points"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        embed = _build_help_page(self.values[0], interaction.user)
        await interaction.response.edit_message(embed=embed)


class HelpView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=180)
        self.uid = uid
        self.add_item(HelpCatSelect())

    async def interaction_check(self, i: discord.Interaction) -> bool:
        return True   # public help — anyone can navigate

    @discord.ui.button(label="🏠 Home",    style=discord.ButtonStyle.secondary, row=1)
    async def home(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=_build_help_page("home", i.user))

    @discord.ui.button(label="🖥️ VPS",    style=discord.ButtonStyle.primary, row=1)
    async def go_vps(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=_build_help_page("vps", i.user))

    @discord.ui.button(label="🌐 Network", style=discord.ButtonStyle.primary, row=1)
    async def go_net(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=_build_help_page("networking", i.user))

    @discord.ui.button(label="💰 Points",  style=discord.ButtonStyle.success, row=1)
    async def go_pts(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=_build_help_page("points", i.user))

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True

# ════════════════════════════════════════════════════════════
#  /deploy  ─  wizard: OS select → confirm → create → DM creds
# ════════════════════════════════════════════════════════════
class DeployOSSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="🖥️  Select Operating System…",
            options=[
                discord.SelectOption(
                    label="🟠  Ubuntu 22.04 LTS", value="ubuntu",
                    description="Recommended — wide compatibility"),
                discord.SelectOption(
                    label="🔵  Debian 12",         value="debian",
                    description="Stable & lightweight"),
            ],
        )

    async def callback(self, i: discord.Interaction):
        v: DeployView = self.view  # type: ignore
        v.os_choice   = self.values[0]
        os_label      = "Ubuntu 22.04 LTS" if v.os_choice == "ubuntu" else "Debian 12"
        uid           = str(i.user.id)
        pts           = users.get(uid, {}).get("points", 0)
        e = _embed("🚀  Confirm Deployment",
                   f"**OS:** {os_label}\n"
                   f"**Specs:** `{DEFAULT_RAM_GB}GB RAM  ·  {DEFAULT_CPU} CPU  ·  {DEFAULT_DISK_GB}GB Disk`\n"
                   f"**Expires in:** `{VPS_LIFETIME_DAYS} days`\n"
                   f"**Cost:** `{POINTS_PER_DEPLOY} pts`  |  **Your Balance:** `{pts} pts`\n\n"
                   "Press **🚀 Deploy** to start, or **❌ Cancel** to abort.",
                   C.VPS)
        v.confirm_btn.disabled = False
        v.os_select.disabled   = True
        await i.response.edit_message(embed=e, view=v)


class DeployView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=90)
        self.uid       = uid
        self.os_choice = "ubuntu"
        self.os_select = DeployOSSelect()
        self.add_item(self.os_select)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your wizard."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🚀  Deploy", style=discord.ButtonStyle.success,
                       disabled=True, row=1)
    async def confirm_btn(self, i: discord.Interaction, btn: discord.ui.Button):
        btn.disabled = True
        for c in self.children:
            c.disabled = True

        uid  = str(i.user.id)
        pts  = users.get(uid, {}).get("points", 0)
        if not is_admin(i.user.id) and pts < POINTS_PER_DEPLOY:
            await i.response.edit_message(
                embed=e_err(f"Not enough points. Need `{POINTS_PER_DEPLOY}` — you have `{pts}`."),
                view=None)
            return

        os_label = "Ubuntu 22.04 LTS" if self.os_choice == "ubuntu" else "Debian 12"

        # ── step 1 ──
        step = _embed("⏳  Deploying…",
                      "```\n[1/4] 🐳  Creating container…\n"
                      "[2/4] ⬜  Installing OS packages…\n"
                      "[3/4] ⬜  Creating credentials…\n"
                      "[4/4] ⬜  Finalizing…\n```", C.INFO)
        await i.response.edit_message(embed=step, view=None)

        rec = await create_vps(i.user.id, os_choice=self.os_choice)

        if "error" in rec:
            await i.edit_original_response(
                embed=e_err(f"Deployment failed: {rec['error']}"))
            return

        if not is_admin(i.user.id):
            users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
            users[uid]["points"] -= POINTS_PER_DEPLOY
            persist_users()

        # ── public success embed (NO credentials) ──
        pub = _embed("🚀  VPS Deployed!",
                     f"{i.user.mention} just launched a **{os_label}** VPS!",
                     C.SUCCESS)
        pub.add_field(name="🆔  Container",   value=f"`{rec['container_id']}`",  inline=True)
        pub.add_field(name="🖥️  OS",           value=f"`{os_label}`",             inline=True)
        pub.add_field(name="🛡️  Systemctl",   value="✅" if rec["systemctl_working"] else "⚠️", inline=True)
        pub.add_field(name="⏰  Expires",      value=f"`{rec['expires_at'][:10]}`", inline=True)
        pub.add_field(name="📬  Credentials", value="✅ Sent to your DMs",        inline=True)
        await i.edit_original_response(embed=pub)

        # ── DM with full creds + Pinggy ──
        dm = _embed("🔐  Your VPS Credentials", color=C.VPS)
        dm.add_field(name="🆔  Container",  value=f"`{rec['container_id']}`",      inline=False)
        dm.add_field(name="🖥️  OS",         value=f"`{os_label}`",                 inline=True)
        dm.add_field(name="💻  Specs",      value=f"`{rec['ram']}GB · {rec['cpu']} CPU · {rec['disk']}GB`", inline=True)
        dm.add_field(name="⏰  Expires",    value=f"`{rec['expires_at'][:10]}`",   inline=True)
        dm.add_field(name="🌐  HTTP",       value=f"`http://{SERVER_IP}:{rec['http_port']}`", inline=False)
        dm.add_field(name="👤  Username",   value=f"```{rec['vps_user']}```",       inline=True)
        dm.add_field(name="🔑  Password",   value=f"```{rec['vps_pass']}```",       inline=True)
        dm.add_field(name="🔗  tmate SSH",  value=f"```{rec['ssh']}```",            inline=False)
        dm.add_field(name="📡  Pinggy (SSH port 22)",
                     value=f"```bash\nssh -p 443 -R0:localhost:22 tcp@a.pinggy.io\n```", inline=False)
        dm.add_field(name="⚠️  Security",  value="Keep these credentials private. Never share your password.", inline=False)
        dm.set_footer(text="Illumix Core — credentials are stored encrypted per-VPS")
        try:
            await i.user.send(embed=dm)
        except Exception:
            pass

        await send_log("VPS Deployed", i.user, rec["container_id"],
                       f"OS:{os_label} RAM:{rec['ram']} CPU:{rec['cpu']}")

    @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=e_warn("Deployment cancelled."), view=None)

# ════════════════════════════════════════════════════════════
#  /admin_create_vps  ─  admin wizard
# ════════════════════════════════════════════════════════════
class AdminSpecsModal(discord.ui.Modal, title="⚙️  VPS Specifications"):
    ram    = discord.ui.TextInput(label="RAM (GB)",    placeholder="e.g. 8",  min_length=1, max_length=4)
    cpu    = discord.ui.TextInput(label="CPU Cores",   placeholder="e.g. 2",  min_length=1, max_length=3)
    disk   = discord.ui.TextInput(label="Disk (GB)",   placeholder="e.g. 20", min_length=1, max_length=5)
    expire = discord.ui.TextInput(label="Expire Days", placeholder="e.g. 15", min_length=1, max_length=3)

    def __init__(self, parent):
        super().__init__()
        self._p = parent

    async def on_submit(self, i: discord.Interaction):
        try:
            r, c, d, e = (int(x.value.strip()) for x in
                          [self.ram, self.cpu, self.disk, self.expire])
        except ValueError:
            return await i.response.send_message(
                embed=e_err("All fields must be whole numbers."), ephemeral=True)

        if not 1 <= r <= 256:
            return await i.response.send_message(embed=e_err("RAM: 1–256 GB"), ephemeral=True)
        if not 1 <= c <= 64:
            return await i.response.send_message(embed=e_err("CPU: 1–64 cores"), ephemeral=True)
        if not 1 <= d <= 2000:
            return await i.response.send_message(embed=e_err("Disk: 1–2000 GB"), ephemeral=True)
        if not 1 <= e <= 365:
            return await i.response.send_message(embed=e_err("Expiry: 1–365 days"), ephemeral=True)

        self._p.ram_v  = r
        self._p.cpu_v  = c
        self._p.disk_v = d
        self._p.exp_v  = e
        os_label       = "Ubuntu 22.04 LTS" if self._p.os_choice == "ubuntu" else "Debian 12"

        confirm = _embed("📋  Confirm VPS Creation", color=C.ADMIN)
        confirm.add_field(name="👤  User",    value=self._p.target.mention, inline=True)
        confirm.add_field(name="🖥️  OS",      value=f"`{os_label}`",        inline=True)
        confirm.add_field(name="🧠  RAM",     value=f"`{r} GB`",            inline=True)
        confirm.add_field(name="⚡  CPU",     value=f"`{c} Cores`",         inline=True)
        confirm.add_field(name="💾  Disk",    value=f"`{d} GB`",            inline=True)
        confirm.add_field(name="⏰  Expiry",  value=f"`{e} days`",          inline=True)

        self._p.confirm_btn.disabled  = False
        self._p.modal_btn.disabled    = True
        await i.response.edit_message(embed=confirm, view=self._p)


class AdminVPSView(discord.ui.View):
    def __init__(self, admin_id: int, target: discord.Member):
        super().__init__(timeout=180)
        self.admin_id  = admin_id
        self.target    = target
        self.os_choice = "ubuntu"
        self.ram_v = self.cpu_v = self.disk_v = self.exp_v = None

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.admin_id:
            await i.response.send_message(embed=e_err("Not your wizard."), ephemeral=True)
            return False
        return True

    @discord.ui.select(
        placeholder="🖥️  Select OS…",
        options=[
            discord.SelectOption(label="🟠  Ubuntu 22.04 LTS", value="ubuntu"),
            discord.SelectOption(label="🔵  Debian 12",         value="debian"),
        ],
        row=0,
    )
    async def os_select(self, i: discord.Interaction, s: discord.ui.Select):
        self.os_choice = s.values[0]
        os_label       = "Ubuntu 22.04 LTS" if self.os_choice == "ubuntu" else "Debian 12"
        e = _embed("⚙️  Enter Specs",
                   f"OS: **{os_label}**\n\nClick **Enter Specs** to set RAM, CPU, Disk & expiry.",
                   C.ADMIN)
        self.modal_btn.disabled = False
        await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="⚙️  Enter Specs", style=discord.ButtonStyle.primary,
                       disabled=True, row=1)
    async def modal_btn(self, i: discord.Interaction, _):
        await i.response.send_modal(AdminSpecsModal(self))

    @discord.ui.button(label="✅  Confirm & Create", style=discord.ButtonStyle.success,
                       disabled=True, row=1)
    async def confirm_btn(self, i: discord.Interaction, btn: discord.ui.Button):
        btn.disabled = True
        for c in self.children:
            c.disabled = True

        os_label = "Ubuntu 22.04 LTS" if self.os_choice == "ubuntu" else "Debian 12"
        pending  = _embed("⏳  Creating VPS…",
                          f"Creating for {self.target.mention} on **{os_label}**…", C.ADMIN)
        await i.response.edit_message(embed=pending, view=self)

        rec = await create_vps(
            self.target.id, ram=self.ram_v, cpu=self.cpu_v, disk=self.disk_v,
            paid=True, expire_days=self.exp_v, os_choice=self.os_choice,
        )
        if "error" in rec:
            await i.edit_original_response(embed=e_err(f"Failed: {rec['error']}"), view=None)
            return

        sc = "✅" if rec["systemctl_working"] else "⚠️"

        # ── public embed (no credentials) ──
        pub = _embed("🛠️  VPS Created!", color=C.ADMIN)
        pub.set_author(name=f"By {i.user.name}", icon_url=i.user.display_avatar.url)
        pub.add_field(name="👤  Owner",     value=self.target.mention,          inline=True)
        pub.add_field(name="🆔  Container", value=f"`{rec['container_id']}`",    inline=True)
        pub.add_field(name="🖥️  OS",        value=f"`{os_label}`",               inline=True)
        pub.add_field(name="💻  Specs",     value=f"`{self.ram_v}GB · {self.cpu_v} CPU · {self.disk_v}GB`", inline=False)
        pub.add_field(name="🛡️  Systemctl", value=sc,                            inline=True)
        pub.add_field(name="⏰  Expires",   value=f"`{rec['expires_at'][:10]}`", inline=True)
        pub.add_field(name="📬  Credentials", value="✅ Sent to user via DM",    inline=True)
        await i.edit_original_response(embed=pub, view=None)

        # ── DM with full creds ──
        dm = _embed("🎁  Your New VPS — Credentials", color=C.ADMIN)
        dm.add_field(name="🆔  Container",  value=f"`{rec['container_id']}`",    inline=False)
        dm.add_field(name="🖥️  OS",         value=f"`{os_label}`",               inline=True)
        dm.add_field(name="💻  Specs",      value=f"`{self.ram_v}GB · {self.cpu_v} CPU · {self.disk_v}GB`", inline=True)
        dm.add_field(name="⏰  Expires",    value=f"`{rec['expires_at'][:10]}`", inline=True)
        dm.add_field(name="🌐  HTTP",       value=f"`http://{SERVER_IP}:{rec['http_port']}`", inline=False)
        dm.add_field(name="👤  Username",   value=f"```{rec['vps_user']}```",    inline=True)
        dm.add_field(name="🔑  Password",   value=f"```{rec['vps_pass']}```",    inline=True)
        dm.add_field(name="🔗  tmate SSH",  value=f"```{rec['ssh']}```",         inline=False)
        dm.add_field(name="📡  Pinggy Tunnel",
                     value="```bash\nssh -p 443 -R0:localhost:22 tcp@a.pinggy.io\n```", inline=False)
        dm.add_field(name="⚠️  Security",  value="Keep these credentials private.", inline=False)
        try:
            await self.target.send(embed=dm)
        except Exception:
            pass

        await send_log("Admin VPS Created", i.user, rec["container_id"],
                       f"For: {self.target.name} OS:{os_label}")

    @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, i: discord.Interaction, _):
        await i.response.edit_message(embed=e_warn("Wizard cancelled."), view=None)

# ════════════════════════════════════════════════════════════
#  /port  ─  real tunnel execution inside Docker container
# # ─────────────────────────────────────────────
# Tunnel helpers
# # Required imports for this section only
# ── Tunnel helpers ───────────────────────────────────────────
async def _kill_existing_tunnel(cid: str, port: int) -> None:
    kill_cmd = (
        f"pkill -f 'R0:localhost:{port}' 2>/dev/null || true; "
        f"pkill -f 'pinggy.*{port}' 2>/dev/null || true"
    )
    await _run("docker", "exec", cid, "bash", "-c", kill_cmd, timeout=10)


async def _ensure_ssh_available(cid: str) -> bool:
    await _run(
        "docker", "exec", cid, "bash", "-c",
        "which ssh >/dev/null 2>&1 || "
        "(apt-get update -qq >/dev/null 2>&1 && "
        "apt-get install -y openssh-client openssh-server -qq >/dev/null 2>&1 && "
        "service ssh start >/dev/null 2>&1)",
        timeout=90,
    )
    return True


async def _run_tunnel_in_container(
    cid: str,
    port: int,
    proto: str
) -> tuple[str | None, str | None]:
    log_path = f"/tmp/tunnel_{port}.log"
    proto_l = proto.lower()

    ssh_flags = (
        "-T -n "
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "-o ServerAliveInterval=30 "
        "-o ExitOnForwardFailure=yes"
    )

    if proto_l == "tcp":
        endpoint = "tcp@a.pinggy.io"
    elif proto_l == "udp":
        endpoint = "udp@a.pinggy.io"
    else:
        endpoint = "a.pinggy.io"

    raw_cmd = (
        f"rm -f {log_path}; "
        f"nohup ssh {ssh_flags} "
        f"-p 443 -R0:localhost:{port} {endpoint} "
        f"> {log_path} 2>&1 &"
    )

    await _run("docker", "exec", cid, "bash", "-c", raw_cmd, timeout=15)

    log_out = ""

    for _ in range(35):
        await asyncio.sleep(1)

        _, log_out, _ = await _run(
            "docker", "exec", cid, "bash", "-c",
            f"cat {log_path} 2>/dev/null",
            timeout=8,
        )

        # exact protocol URL
        if proto_l == "tcp":
            m = re.search(r"tcp://[^\s]+", log_out, re.IGNORECASE)
            if m:
                return m.group(0), None

        elif proto_l == "udp":
            m = re.search(r"udp://[^\s]+", log_out, re.IGNORECASE)
            if m:
                return m.group(0), None

        else:
            matches = re.findall(r"https?://[^\s]+", log_out, re.IGNORECASE)
            for url in matches:
                if "dashboard.pinggy.io" not in url:
                    return url, None

        # allocated port fallback
        m_alloc = re.search(
            r"allocated\s+port\s+([1-9]\d{1,5})\s+for\s+remote",
            log_out,
            re.IGNORECASE,
        )

        if m_alloc:
            remote_port = int(m_alloc.group(1))

            host_match = re.search(
                r"([a-zA-Z0-9\-\.]+\.pinggy(?:-free)?\.link)",
                log_out,
                re.IGNORECASE,
            )
            host = host_match.group(1) if host_match else "a.pinggy.io"

            if proto_l == "tcp":
                return f"tcp://{host}:{remote_port}", None
            elif proto_l == "udp":
                return f"udp://{host}:{remote_port}", None
            else:
                return f"https://{host}:{remote_port}", None

    debug_path = os.path.join(DATA_DIR, f"tunnel_debug_{cid}_{port}.log")

    try:
        with open(debug_path, "w", encoding="utf-8") as fh:
            fh.write(log_out or "(empty)")
    except Exception:
        pass

    return None, (
        "The tunnel could not be established.\n"
        f"📬 Debug saved: `{debug_path}`\n\n"
        f"```\n{(log_out[:800] or 'No output')}\n```"
    )
# ────────────────────────────────────────────────────────────
# Port Modal
# ────────────────────────────────────────────────────────────
class PortModal(discord.ui.Modal, title="🔌 Enter Internal Port"):
    port_number = discord.ui.TextInput(
        label="Internal Port",
        placeholder="22 / 25565 / 8080",
        min_length=1,
        max_length=5,
    )

    def __init__(self, cid: str, protocol: str, uid: int):
        super().__init__()
        self.cid = cid
        self.protocol = protocol
        self.uid = uid

    async def on_submit(self, i: discord.Interaction):
        try:
            port = int(self.port_number.value.strip())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            return await i.response.send_message(
                embed=e_err("Port must be 1-65535."),
                ephemeral=True,
            )

        vps = vps_db.get(self.cid)
        if not vps:
            return await i.response.send_message(
                embed=e_err("VPS not found."),
                ephemeral=True,
            )

        await i.response.send_message(
            embed=_embed(
                "⏳ Setting Up Tunnel...",
                f"**VPS:** `{self.cid}`\n"
                f"**Protocol:** `{self.protocol.upper()}`\n"
                f"**Port:** `{port}`",
                C.INFO,
            )
        )

        await _kill_existing_tunnel(self.cid, port)
        await _ensure_ssh_available(self.cid)

        public_url, err = await _run_tunnel_in_container(
            self.cid,
            port,
            self.protocol
        )

        if not public_url:
            return await i.edit_original_response(embed=e_err(err))

        # save active tunnel
        active_tunnels[f"{self.cid}:{port}"] = {
            "proto": self.protocol.upper(),
            "port": port,
            "public_url": public_url,
            "cid": self.cid,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        save_json(os.path.join(DATA_DIR, "tunnels.json"), active_tunnels)

        await i.edit_original_response(
            embed=_embed(
                "✅ Port Forwarded Successfully",
                f"**VPS:** `{self.cid}`\n"
                f"**Protocol:** `{self.protocol.upper()}`\n"
                f"**Internal Port:** `{port}`\n\n"
                "📬 Full details sent to your DMs.",
                C.SUCCESS,
            )
        )

        created_at = datetime.now(timezone.utc)
        expiry_at = created_at + timedelta(minutes=60)

        username = vps.get("vps_user") or vps.get("username") or "root"
        password = vps.get("vps_pass") or vps.get("password") or "Not Available"
        ssh_cmd = vps.get("ssh") or "Not Available"

        dm = _embed("🌐 Port Forward Ready", color=C.PREMIUM)
        dm.add_field(name="🖥️ VPS", value=f"`{self.cid}`", inline=True)
        dm.add_field(name="📡 Protocol", value=f"`{self.protocol.upper()}`", inline=True)
        dm.add_field(name="🔌 Internal Port", value=f"`{port}`", inline=True)
        dm.add_field(name="🌍 Public URL", value=f"```\n{public_url}\n```", inline=False)
        dm.add_field(name="👤 Username", value=f"```{username}```", inline=True)
        dm.add_field(name="🔑 Password", value=f"```{password}```", inline=True)
        dm.add_field(name="🔗 SSH", value=f"```{ssh_cmd}```", inline=False)
        dm.add_field(name="🕒 Created", value=f"`{created_at}`", inline=True)
        dm.add_field(name="⏰ Expires", value=f"`{expiry_at}`", inline=True)
        dm.add_field(name="📊 Status", value="`🟢 Active`", inline=True)
        dm.add_field(
            name="⚠️ Note",
            value=(
                "Free tunnels expire after ~60 min.\n"
                "Run the reconnect command inside your VPS to refresh."
            ),
            inline=False,
        )

        dm_sent = False
        try:
            await i.user.send(embed=dm)
            dm_sent = True
        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"DM send failed: {e}")

        if not dm_sent:
            fail_pub = _embed(
                "✅ Port Forwarded — ⚠️ DM Failed",
                f"**VPS:** `{self.cid}` | "
                f"**Protocol:** `{self.protocol.upper()}` | "
                f"**Port:** `{port}`\n\n"
                "❌ Please enable DMs to receive tunnel details.\n"
                "*(Server Settings → Allow Direct Messages)*",
                C.WARNING,
            )
            await i.edit_original_response(embed=fail_pub)

        # ── track port ──
        vps.setdefault("additional_ports", [])
        if port not in vps["additional_ports"]:
            vps["additional_ports"].append(port)
            persist_vps()

        await send_log(
            "Port Forward", i.user, self.cid,
            f"Proto:{self.protocol.upper()} Port:{port} URL:{public_url or 'FAILED'}",
        )


# ── Protocol selector ────────────────────────────────────────
class PortProtocolView(discord.ui.View):
    def __init__(self, uid: int, cid: str):
        super().__init__(timeout=60)
        self.uid = uid
        self.cid = cid

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your wizard."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🔷  TCP",  style=discord.ButtonStyle.primary,   row=0)
    async def tcp(self,  i, _): await i.response.send_modal(PortModal(self.cid, "TCP",  self.uid))

    @discord.ui.button(label="🟣  UDP",  style=discord.ButtonStyle.secondary, row=0)
    async def udp(self,  i, _): await i.response.send_modal(PortModal(self.cid, "UDP",  self.uid))

    @discord.ui.button(label="🌐  HTTP", style=discord.ButtonStyle.success,   row=0)
    async def http(self, i, _): await i.response.send_modal(PortModal(self.cid, "HTTP", self.uid))

    @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.danger,  row=1)
    async def cancel(self, i, _):
        await i.response.edit_message(embed=e_warn("Port wizard closed."), view=None)


async def _port_vps_picked(i: discord.Interaction, cid: str):
    vps = vps_db.get(cid)
    if not vps:
        return await i.response.edit_message(embed=e_err("VPS not found."), view=None)
    if not owns_vps(i.user.id, cid):
        return await i.response.edit_message(
            embed=e_err("❌  You cannot manage another user's VPS ports."), view=None)
    if vps.get("suspended"):
        return await i.response.edit_message(
            embed=e_err("VPS is suspended — renew it first."), view=None)

    os_icon = "🟠" if vps.get("os", "ubuntu") == "ubuntu" else "🔵"
    e = _embed("🌐  Port Forwarding — Select Protocol",
               f"**VPS:** {os_icon} `{cid}`\n\nChoose the tunnel protocol:", C.PREMIUM)
    e.add_field(name="🔷 TCP",  value="Game servers, SSH, databases", inline=True)
    e.add_field(name="🟣 UDP",  value="Game servers, VoIP, media",    inline=True)
    e.add_field(name="🌐 HTTP", value="Web apps, APIs, dashboards",    inline=True)
    await i.response.edit_message(embed=e, view=PortProtocolView(i.user.id, cid))


class PortVPSView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid
        self.add_item(OwnedVPSSelect(uid, _port_vps_picked))

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your wizard."), ephemeral=True)
            return False
        return True

# ════════════════════════════════════════════════════════════
#  /manage  ─  wizard control panel
# ════════════════════════════════════════════════════════════
class ManageSelectView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid
        self.add_item(OwnedVPSSelect(uid, _open_manage_panel, "Select VPS to manage…"))

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your wizard."), ephemeral=True)
            return False
        return True


async def _open_manage_panel(i: discord.Interaction, cid: str):
    if not can_manage_vps(i.user.id, cid):
        return await i.response.edit_message(
            embed=e_err("You don't have permission to manage this VPS."), view=None)
    vps = vps_db[cid]
    embed, view = _build_manage_embed_view(cid, vps, i.user)
    await i.response.edit_message(embed=embed, view=view)


def _build_manage_embed_view(
    cid: str, vps: dict, user: discord.User
) -> tuple[discord.Embed, discord.ui.View]:
    expires  = datetime.fromisoformat(vps["expires_at"])
    now      = datetime.now(timezone.utc)
    tl       = expires - now if expires > now else timedelta(0)
    d, h, m  = tl.days, tl.seconds // 3600, (tl.seconds % 3600) // 60

    if vps.get("suspended"):
        status, color = "⏸️  Suspended", C.WARNING
    elif not vps["active"]:
        status, color = "🔴  Stopped",   C.ERROR
    else:
        status, color = "🟢  Running",   C.SUCCESS

    os_icon  = "🟠" if vps.get("os", "ubuntu") == "ubuntu" else "🔵"
    time_str = f"{d}d {h}h {m}m" if tl.total_seconds() > 0 else "⚠️  EXPIRED"
    temo     = "🟢" if d > 7 else "🟡" if d > 3 else "🔴"

    e = _embed("🚀  VPS Control Panel", f"**Container:** `{cid}`", color)
    e.set_thumbnail(url=user.display_avatar.url)
    e.add_field(name="📊  Status",
                value=f"```\nStatus:    {status}\nOS:        {vps.get('os','ubuntu').capitalize()}\n"
                      f"Systemctl: {'✅' if vps.get('systemctl_working') else '❌'}\n"
                      f"Type:      {'🎁 Giveaway' if vps.get('giveaway_vps') else '💎 Premium'}\n```",
                inline=False)
    e.add_field(name="⏰  Expiry",
                value=f"```\n{temo} {time_str}\n{vps['expires_at'][:10]}\n```", inline=True)
    e.add_field(name="🌐  HTTP",
                value=f"`http://{SERVER_IP}:{vps['http_port']}`", inline=True)
    if vps.get("additional_ports"):
        e.add_field(name="🔌  Ports",
                    value="`" + ", ".join(map(str, vps["additional_ports"])) + "`", inline=True)
    if vps.get("suspended"):
        e.add_field(name="⚠️  Action Required",
                    value="Click **⏳ Renew** to reactivate your VPS.", inline=False)
    return e, ManageControlView(cid)


class ManageControlView(discord.ui.View):
    def __init__(self, cid: str):
        super().__init__(timeout=300)
        self.cid = cid

    @property
    def vps(self): return vps_db.get(self.cid)

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if not can_manage_vps(i.user.id, self.cid):
            await i.response.send_message(
                embed=e_err("❌  You don't have permission to manage this VPS."),
                ephemeral=True)
            return False
        return True

    # ── row 0: power ──────────────────────────────────────
    @discord.ui.button(label="▶ Start",   emoji="🟢", style=discord.ButtonStyle.success,   row=0)
    async def start(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if v.get("suspended"):
            return await i.followup.send(embed=e_err("Suspended — renew first."), ephemeral=True)
        if v["active"]:
            return await i.followup.send(embed=e_warn("Already running."), ephemeral=True)
        if await docker_start(self.cid):
            v["active"] = True; persist_vps()
            await send_log("VPS Started", i.user, self.cid)
            e = e_ok(f"`{self.cid}` is now running.", "🟢  Started")
            e.add_field(name="🌐  HTTP", value=f"`http://{SERVER_IP}:{v['http_port']}`")
            await i.followup.send(embed=e, ephemeral=True)
        else:
            await i.followup.send(embed=e_err("Failed to start."), ephemeral=True)

    @discord.ui.button(label="⏹ Stop",   emoji="🔴", style=discord.ButtonStyle.danger,    row=0)
    async def stop(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if not v["active"]:
            return await i.followup.send(embed=e_warn("Already stopped."), ephemeral=True)
        if await docker_stop(self.cid):
            v["active"] = False; persist_vps()
            await send_log("VPS Stopped", i.user, self.cid)
            await i.followup.send(embed=e_warn(f"`{self.cid}` stopped.", "🔴  Stopped"), ephemeral=True)
        else:
            await i.followup.send(embed=e_err("Failed to stop."), ephemeral=True)

    @discord.ui.button(label="🔄 Restart", style=discord.ButtonStyle.primary, row=0)
    async def restart(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if v.get("suspended"):
            return await i.followup.send(embed=e_err("Suspended — renew first."), ephemeral=True)
        if await docker_restart(self.cid):
            v["active"] = True; v["suspended"] = False; persist_vps()
            await send_log("VPS Restarted", i.user, self.cid)
            e = e_ok(f"`{self.cid}` restarted.", "🔄  Restarted")
            await i.followup.send(embed=e, ephemeral=True)
        else:
            await i.followup.send(embed=e_err("Failed to restart."), ephemeral=True)

    # ── row 1: management ─────────────────────────────────
    @discord.ui.button(label="⏳ Renew",  emoji="🔁", style=discord.ButtonStyle.success, row=1)
    async def renew(self, i, _):
        v   = self.vps
        uid = str(i.user.id)
        if v.get("giveaway_vps"):
            return await i.response.send_message(
                embed=e_warn("Giveaway VPS cannot be renewed."), ephemeral=True)
        users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
        mode = renew_mode.get("mode", "15")
        cost = POINTS_RENEW_15 if mode == "15" else POINTS_RENEW_30
        days = 15 if mode == "15" else 30
        if users[uid]["points"] < cost:
            return await i.response.send_message(
                embed=e_err(f"Need `{cost}` pts. You have `{users[uid]['points']}`."),
                ephemeral=True)
        cur_exp = datetime.fromisoformat(v["expires_at"])
        new_exp = max(datetime.now(timezone.utc), cur_exp) + timedelta(days=days)
        ce = _embed("🔄  Confirm Renewal",
                    f"Renew `{self.cid}` for **{days} days**?\n"
                    f"Cost: `{cost} pts`  →  Expiry: `{new_exp.strftime('%Y-%m-%d')}`",
                    C.PREMIUM)
        cv = discord.ui.View(timeout=60)

        _cid_renew = self.cid
        async def do_renew_cb(ci: discord.Interaction, __):
            if ci.user.id != i.user.id:
                return await ci.response.send_message(embed=e_err("Not yours."), ephemeral=True)
            await ci.response.defer(ephemeral=True)
            users[uid]["points"] -= cost
            persist_users()
            v["expires_at"] = new_exp.isoformat()
            v["active"]     = True
            v["suspended"]  = False
            persist_vps()
            await docker_start(_cid_renew)
            await send_log("VPS Renewed", i.user, _cid_renew, f"+{days}d")
            ee = e_ok(f"`{_cid_renew}` renewed for **{days} days**!", "✅  Renewed")
            ee.add_field(name="📆  New Expiry", value=f"`{new_exp.strftime('%Y-%m-%d')}`")
            await ci.followup.send(embed=ee, ephemeral=True)

        async def cancel_renew_cb(ci: discord.Interaction, __):
            await ci.response.send_message(embed=e_ok("Cancelled."), ephemeral=True)

        do_renew     = discord.ui.Button(label="✅  Confirm", style=discord.ButtonStyle.success)
        cancel_renew = discord.ui.Button(label="❌  Cancel",  style=discord.ButtonStyle.secondary)
        do_renew.callback     = do_renew_cb
        cancel_renew.callback = cancel_renew_cb
        cv.add_item(do_renew); cv.add_item(cancel_renew)
        await i.response.send_message(embed=ce, view=cv, ephemeral=True)

    @discord.ui.button(label="⏰ Time Left", style=discord.ButtonStyle.secondary, row=1)
    async def timeleft(self, i, _):
        v       = self.vps
        expires = datetime.fromisoformat(v["expires_at"])
        tl      = expires - datetime.now(timezone.utc)
        if tl.total_seconds() > 0:
            d, h, m = tl.days, tl.seconds // 3600, (tl.seconds % 3600) // 60
            pct     = min((d / 15) * 100, 100)
            bar     = "🟢" * int(pct / 20) + "⚫" * (5 - int(pct / 20))
            e = _embed("⏰  Time Remaining", f"`{self.cid}`", C.INFO)
            e.add_field(name="⏳  Remaining",
                        value=f"```{bar} {pct:.0f}%\n{d}d {h}h {m}m```", inline=False)
            e.add_field(name="📆  Expiry", value=f"`{expires.strftime('%Y-%m-%d %H:%M UTC')}`")
        else:
            e = e_err("VPS has expired. Use **Renew** to reactivate.", "❌  Expired")
        await i.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="🔑 SSH",    style=discord.ButtonStyle.secondary, row=1)
    async def reset_ssh(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if v.get("suspended"):
            return await i.followup.send(embed=e_err("Suspended — renew first."), ephemeral=True)
        ssh = await docker_exec_capture_ssh(self.cid)
        v["ssh"] = ssh; persist_vps()
        await send_log("SSH Reset", i.user, self.cid)
        e = e_ok("New SSH session started. Check your DMs.", "🔑  SSH Reset")
        await i.followup.send(embed=e, ephemeral=True)
        # DM SSH
        dm = _embed("🔑  SSH Details", color=C.VPS)
        dm.add_field(name="🔗  SSH",      value=f"```{ssh}```",           inline=False)
        dm.add_field(name="👤  Username", value=f"```{v['vps_user']}```", inline=True)
        dm.add_field(name="🔑  Password", value=f"```{v['vps_pass']}```", inline=True)
        try:
            await i.user.send(embed=dm)
        except Exception:
            pass

    # ── row 2: advanced ───────────────────────────────────
    @discord.ui.button(label="📊 Monitor", style=discord.ButtonStyle.primary,   row=2)
    async def monitor(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if v.get("suspended"):
            return await i.followup.send(embed=e_err("Suspended."), ephemeral=True)
        stats = await docker_stats(self.cid)
        e = _embed("📊  Live VPS Monitor", f"`{self.cid}`", C.INFO)
        if stats:
            e.add_field(name="⚡  CPU",    value=f"```{stats['cpu']}```",               inline=True)
            e.add_field(name="🧠  RAM",    value=f"```{stats['mem_usage']}\n({stats['mem_perc']})```", inline=True)
            e.add_field(name="🌐  Net IO", value=f"```{stats['net_io']}```",            inline=True)
        else:
            e.add_field(name="⚠️  Stats Unavailable",
                        value="VPS may be starting up. Try again in a moment.", inline=False)
        e.set_footer(text="Illumix Core  •  Click again to refresh")
        await i.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="💾 Reinstall", style=discord.ButtonStyle.secondary, row=2)
    async def reinstall(self, i, _):
        await i.response.defer(ephemeral=True)
        v = self.vps
        if v.get("suspended"):
            return await i.followup.send(embed=e_err("Suspended — renew first."), ephemeral=True)
        warn = e_warn(
            "**ALL DATA will be permanently deleted** and the OS will be reinstalled.\n"
            "Your expiry date is preserved.", "⚠️  Confirm Reinstall")
        warn.add_field(name="🆔  Container", value=f"`{self.cid}`")
        cv = discord.ui.View(timeout=60)

        _cid_reinstall = self.cid
        async def do_reinstall_cb(ci: discord.Interaction, __):
            if ci.user.id != i.user.id:
                return await ci.response.send_message(embed=e_err("Not yours."), ephemeral=True)
            await ci.response.defer(ephemeral=True)
            old_expiry = v["expires_at"]
            await docker_stop(_cid_reinstall); await docker_remove(_cid_reinstall)
            rec = await create_vps(int(v["owner"]), ram=v["ram"], cpu=v["cpu"],
                                   disk=v["disk"], os_choice=v.get("os", "ubuntu"))
            if "error" in rec:
                return await ci.followup.send(embed=e_err(f"Reinstall failed: {rec['error']}"), ephemeral=True)
            vps_db.pop(_cid_reinstall, None)
            rec["expires_at"] = old_expiry
            vps_db[rec["container_id"]] = rec
            persist_vps()
            ee = e_ok("OS reinstalled. New credentials sent to your DMs.", "✅  Reinstalled")
            ee.add_field(name="🆔  New ID", value=f"`{rec['container_id']}`")
            await ci.followup.send(embed=ee, ephemeral=True)
            dm = _embed("🔐  New Credentials After Reinstall", color=C.VPS)
            dm.add_field(name="🆔  Container", value=f"`{rec['container_id']}`", inline=False)
            dm.add_field(name="👤  Username",  value=f"```{rec['vps_user']}```", inline=True)
            dm.add_field(name="🔑  Password",  value=f"```{rec['vps_pass']}```", inline=True)
            dm.add_field(name="🔗  SSH",       value=f"```{rec['ssh']}```",      inline=False)
            try:
                await i.user.send(embed=dm)
            except Exception:
                pass

        async def cancel_reinstall_cb(ci: discord.Interaction, __):
            await ci.response.send_message(embed=e_ok("Cancelled."), ephemeral=True)

        do_reinstall     = discord.ui.Button(label="✅  Confirm", style=discord.ButtonStyle.danger)
        cancel_reinstall = discord.ui.Button(label="❌  Cancel",  style=discord.ButtonStyle.secondary)
        do_reinstall.callback     = do_reinstall_cb
        cancel_reinstall.callback = cancel_reinstall_cb
        cv.add_item(do_reinstall); cv.add_item(cancel_reinstall)
        await i.followup.send(embed=warn, view=cv, ephemeral=True)

    @discord.ui.button(label="🗑 Delete",  style=discord.ButtonStyle.danger,     row=2)
    async def delete(self, i, _):
        if not owns_vps(i.user.id, self.cid):
            return await i.response.send_message(
                embed=e_err("Only the owner can delete this VPS."), ephemeral=True)
        warn = e_warn(
            f"Delete `{self.cid}`?\n**This is permanent.** You'll receive a 50% point refund.",
            "⚠️  Confirm Delete")
        cv = discord.ui.View(timeout=30)

        _cid_del = self.cid
        _vps_del = self.vps
        async def do_delete_cb(ci: discord.Interaction, __):
            if ci.user.id != i.user.id:
                return await ci.response.send_message(embed=e_err("Not yours."), ephemeral=True)
            await ci.response.defer(ephemeral=True)
            v = _vps_del
            uid = str(i.user.id)
            await docker_remove(_cid_del)
            refund = POINTS_PER_DEPLOY // 2
            if not v.get("giveaway_vps") and not is_admin(i.user.id):
                users.setdefault(uid, {"points": 0})
                users[uid]["points"] += refund
                persist_users()
            vps_db.pop(_cid_del, None)
            persist_vps()
            await send_log("VPS Deleted", i.user, _cid_del)
            ee = e_ok(f"VPS `{_cid_del}` deleted. `+{refund}` pts refunded.", "🗑️  Deleted")
            await ci.followup.send(embed=ee, ephemeral=True)

        async def cancel_delete_cb(ci: discord.Interaction, __):
            await ci.response.send_message(embed=e_ok("Cancelled."), ephemeral=True)

        do_delete     = discord.ui.Button(label="🗑️  Confirm Delete", style=discord.ButtonStyle.danger)
        cancel_delete = discord.ui.Button(label="❌  Cancel",          style=discord.ButtonStyle.secondary)
        do_delete.callback     = do_delete_cb
        cancel_delete.callback = cancel_delete_cb

        cv.add_item(do_delete); cv.add_item(cancel_delete)
        await i.response.send_message(embed=warn, view=cv, ephemeral=True)

# ════════════════════════════════════════════════════════════
#  /status  ─  public, clean health summary, auto-refresh
# ════════════════════════════════════════════════════════════
def _build_status_embed() -> discord.Embed:
    total      = len(vps_db)
    active     = len([v for v in vps_db.values() if v["active"] and not v.get("suspended")])
    suspended  = len([v for v in vps_db.values() if v.get("suspended")])
    total_users_with_vps = len({v["owner"] for v in vps_db.values() if v["active"]})
    total_users_count    = len(users)

    # Health scoring
    score = 100
    if total > 0 and suspended / total > 0.5:
        score -= 30
    elif total > 0 and suspended / total > 0.2:
        score -= 10
    ss = sys_stats()
    if ss:
        if ss["cpu"] > 90:   score -= 30
        elif ss["cpu"] > 70: score -= 10
        if ss["ram_pct"] > 90:   score -= 30
        elif ss["ram_pct"] > 75: score -= 10

    if score >= 80:
        health_str = "🟢  **Good** — All systems operational"
        color      = C.SUCCESS
    elif score >= 50:
        health_str = "🟡  **Warning** — Elevated activity"
        color      = C.WARNING
    else:
        health_str = "🔴  **Critical** — Action required"
        color      = C.ERROR

    e = _embed("📊  System Status", color=color)
    e.add_field(name="🏥  System Health",        value=health_str,                    inline=False)
    e.add_field(name="🖥️  Total VPS Deployed",   value=f"```{total}```",              inline=True)
    e.add_field(name="👥  Users with Active VPS", value=f"```{total_users_with_vps}```", inline=True)
    e.add_field(name="👤  Registered Users",      value=f"```{total_users_count}```", inline=True)

    uptime = int(time.time() - BOT_START_TIME)
    h, rem = divmod(uptime, 3600)
    m, s   = divmod(rem, 60)
    e.add_field(name="⏱️  Bot Uptime",    value=f"`{h}h {m}m {s}s`", inline=True)
    e.add_field(name="🔄  Auto-Refresh",  value="Updates every `60s`",  inline=True)
    e.set_footer(text=f"Illumix Core  •  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return e


class StatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)
        self.msg: discord.Message | None = None

    @discord.ui.button(label="🔄  Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, i, _):
        await i.response.edit_message(embed=_build_status_embed(), view=self)

    async def on_timeout(self):
        for c in self.children:
            c.disabled = True
        if self.msg:
            try:
                await self.msg.edit(view=self)
            except Exception:
                pass

# ════════════════════════════════════════════════════════════
#  GAME HELPERS
# ════════════════════════════════════════════════════════════
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

# Win-condition descriptors for flavour text
RPS_WIN_DESC = {
    ("rock",     "scissors"): "Rock crushes Scissors!",
    ("paper",    "rock"):     "Paper covers Rock!",
    ("scissors", "paper"):    "Scissors cut Paper!",
}


def _game_on_cooldown(uid: int, cooldown: float) -> float:
    """Return remaining cooldown seconds, or 0 if ready."""
    last = game_cooldowns.get(uid, 0.0)
    remaining = cooldown - (time.time() - last)
    return max(0.0, remaining)


def _award_points(uid_int: int, pts: int) -> int:
    """Add pts to user, persist, return new total."""
    uid = str(uid_int)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    users[uid]["points"] += pts
    persist_users()
    return users[uid]["points"]


# ════════════════════════════════════════════════════════════
#  Rock Paper Scissors  — with streaks, cooldown, bonus pts
# ════════════════════════════════════════════════════════════
class RPSView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=60)
        self.uid = uid

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your game!"), ephemeral=True)
            return False
        return True

    async def _play(self, i: discord.Interaction, choice: str):
        # cooldown guard
        remaining = _game_on_cooldown(i.user.id, GAME_COOLDOWN_SECS)
        if remaining > 0:
            return await i.response.send_message(
                embed=e_warn(f"⏳  Cooldown — wait **{remaining:.1f}s** before playing again."),
                ephemeral=True,
            )
        game_cooldowns[i.user.id] = time.time()

        for c in self.children:
            c.disabled = True

        bot_choice = random.choice(["rock", "paper", "scissors"])
        p_emoji    = RPS_EMOJI[choice]
        b_emoji    = RPS_EMOJI[bot_choice]

        if choice == bot_choice:
            # Draw
            rps_streaks[i.user.id] = 0
            result  = "🤝  Draw!"
            color   = C.WARNING
            pts     = 0
            flavor  = "It's a tie — no points awarded."
            streak_msg = ""

        elif RPS_BEATS[choice] == bot_choice:
            # Win
            rps_streaks[i.user.id] = rps_streaks.get(i.user.id, 0) + 1
            streak = rps_streaks[i.user.id]
            bonus  = RPS_STREAK_BONUS if streak % 3 == 0 else 0
            pts    = RPS_WIN_PTS + bonus
            result = "🎉  You Win!"
            color  = C.SUCCESS
            flavor = RPS_WIN_DESC.get((choice, bot_choice), "")
            streak_msg = (
                f"\n🔥  **{streak}-win streak!**  +{bonus} bonus pts!"
                if bonus else
                f"\n🔥  **{streak}-win streak!**" if streak > 1 else ""
            )
        else:
            # Lose
            rps_streaks[i.user.id] = 0
            result     = "💀  Bot Wins!"
            color      = C.ERROR
            pts        = 0
            flavor     = RPS_WIN_DESC.get((bot_choice, choice), "")
            streak_msg = ""

        e = _embed(
            f"🪨📄✂️  Rock Paper Scissors — {result}",
            f"{p_emoji} **You:** {choice.capitalize()}\n"
            f"{b_emoji} **Bot:** {bot_choice.capitalize()}\n\n"
            f"*{flavor}*{streak_msg}",
            color,
        )

        if pts:
            new_bal = _award_points(i.user.id, pts)
            e.add_field(name="🏆  Points Earned", value=f"`+{pts} pts`",         inline=True)
            e.add_field(name="💳  New Balance",   value=f"`{new_bal} pts`",       inline=True)
            e.add_field(name="🔁  Play Again?",
                        value=f"Cooldown: `{GAME_COOLDOWN_SECS}s`",              inline=True)
            # DM reward
            try:
                dm = e_ok(
                    f"You beat the bot at **Rock Paper Scissors** and earned **{pts} pts**! 🎉\n"
                    f"New balance: `{new_bal} pts`",
                    "🎮  Game Reward",
                )
                await i.user.send(embed=dm)
            except Exception:
                pass
        else:
            e.add_field(name="🔁  Play Again?",
                        value=f"Cooldown: `{GAME_COOLDOWN_SECS}s`", inline=True)

        # Add "Play Again" button
        e.set_footer(text=f"Illumix Core  •  Streak: {rps_streaks.get(i.user.id, 0)} wins")
        await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="🪨  Rock",     style=discord.ButtonStyle.secondary, row=0)
    async def rock(self,     i, _): await self._play(i, "rock")

    @discord.ui.button(label="📄  Paper",    style=discord.ButtonStyle.secondary, row=0)
    async def paper(self,    i, _): await self._play(i, "paper")

    @discord.ui.button(label="✂️  Scissors", style=discord.ButtonStyle.secondary, row=0)
    async def scissors(self, i, _): await self._play(i, "scissors")

    @discord.ui.button(label="🏠  Menu",     style=discord.ButtonStyle.primary,   row=1)
    async def back_menu(self, i, _):
        uid = str(i.user.id)
        pts = users.get(uid, {}).get("points", 0)
        e   = _build_game_menu_embed(i.user, pts)
        await i.response.edit_message(embed=e, view=GameMenuView(i.user.id))


# ════════════════════════════════════════════════════════════
#  Number Guess modal
# ════════════════════════════════════════════════════════════
class NumberGuessModal(discord.ui.Modal, title="🔢  Guess a Number (1–10)"):
    guess = discord.ui.TextInput(
        label="Your Guess", placeholder="Enter 1 – 10",
        min_length=1, max_length=2,
    )

    def __init__(self, uid: int):
        super().__init__()
        self.uid = uid

    async def on_submit(self, i: discord.Interaction):
        remaining = _game_on_cooldown(i.user.id, GAME_COOLDOWN_SECS)
        if remaining > 0:
            return await i.response.send_message(
                embed=e_warn(f"⏳  Wait **{remaining:.1f}s** before playing again."),
                ephemeral=True,
            )
        game_cooldowns[i.user.id] = time.time()

        try:
            g = int(self.guess.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=e_err("Enter a whole number from 1 to 10."), ephemeral=True)
        if not 1 <= g <= 10:
            return await i.response.send_message(
                embed=e_err("Number must be 1–10."), ephemeral=True)

        answer = random.randint(1, 10)
        if g == answer:
            new_bal = _award_points(i.user.id, NUM_GUESS_PTS)
            e = e_ok(
                f"🎉  Correct! The number was **{answer}**!\n"
                f"`+{NUM_GUESS_PTS} pts`  →  balance: `{new_bal} pts`",
                "🎮  Correct!",
            )
            try:
                dm = e_ok(
                    f"You guessed **{answer}** correctly in Number Guess!\n"
                    f"Earned **{NUM_GUESS_PTS} pts**.  Balance: `{new_bal} pts`",
                    "🎮  Number Guess Reward",
                )
                await i.user.send(embed=dm)
            except Exception:
                pass
        else:
            diff = abs(g - answer)
            hint = "🔥 Very close!" if diff <= 1 else ("👍 Close!" if diff <= 3 else "❄️ Way off!")
            e = _embed(
                "🎮  Wrong Guess!",
                f"You guessed `{g}`. The answer was **{answer}**.\n{hint}",
                C.ERROR,
            )
        await i.response.send_message(embed=e)


# ════════════════════════════════════════════════════════════
#  Game menu helpers
# ════════════════════════════════════════════════════════════
def _build_game_menu_embed(user: discord.User, pts: int) -> discord.Embed:
    streak = rps_streaks.get(user.id, 0)
    e = _embed(
        "🎮  Illumix Game Center",
        f"**Balance:** `{pts} pts`  |  **RPS Streak:** `{streak}` wins\n\n"
        "Pick a game below:",
        C.GAME,
    )
    e.add_field(name="🪨📄✂️  Rock Paper Scissors",
                value=f"Win → **+{RPS_WIN_PTS} pts**\nEvery 3 wins → **+{RPS_STREAK_BONUS} bonus pts**", inline=True)
    e.add_field(name="🔢  Number Guess (1–10)",
                value=f"Correct → **+{NUM_GUESS_PTS} pts**", inline=True)
    e.add_field(name="🪙  Coin Flip",
                value="Bet points → **1.8× payout** or lose all", inline=True)
    e.add_field(name="🍀  Lucky Box",
                value="Spin for **0–100 pts** mystery reward", inline=True)
    e.set_footer(text=f"Illumix Core  •  Cooldown: {GAME_COOLDOWN_SECS}s per game")
    return e


class GameMenuView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=120)
        self.uid = uid

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your game menu."), ephemeral=True)
            return False
        return True

    @discord.ui.button(label="🪨  Rock Paper Scissors",
                       style=discord.ButtonStyle.primary, row=0)
    async def rps(self, i, _):
        streak = rps_streaks.get(i.user.id, 0)
        e = _embed(
            "🪨📄✂️  Rock Paper Scissors",
            f"Choose your move!\n"
            f"Win → **+{RPS_WIN_PTS} pts** · Every 3-win streak → **+{RPS_STREAK_BONUS} bonus**\n"
            f"🔥 Current streak: **{streak}**",
            C.GAME,
        )
        await i.response.edit_message(embed=e, view=RPSView(i.user.id))

    @discord.ui.button(label="🔢  Number Guess",
                       style=discord.ButtonStyle.success, row=0)
    async def numguess(self, i, _):
        await i.response.send_modal(NumberGuessModal(i.user.id))

    @discord.ui.button(label="🪙  Coin Flip",
                       style=discord.ButtonStyle.primary, row=1)
    async def coinflip_btn(self, i, _):
        uid = str(i.user.id)
        pts = users.get(uid, {}).get("points", 0)
        if pts <= 0:
            return await i.response.send_message(
                embed=e_err("You need at least **1 pt** to flip a coin."), ephemeral=True)
        await i.response.send_modal(CoinFlipModal(i.user.id))

    @discord.ui.button(label="🍀  Lucky Box",
                       style=discord.ButtonStyle.success, row=1)
    async def lucky_btn(self, i, _):
        remaining = _game_on_cooldown(i.user.id, LUCK_COOLDOWN_SECS)
        if remaining > 0:
            return await i.response.send_message(
                embed=e_warn(f"⏳  Lucky Box cooldown — wait **{remaining:.0f}s**."),
                ephemeral=True,
            )
        e = _embed("🍀  Lucky Box",
                   "Choose your box and see what fate has in store!", C.GAME)
        e.add_field(name="🟦  Mystery Chest",  value="Low risk, steady reward", inline=True)
        e.add_field(name="🟨  Golden Box",     value="Medium risk, big reward", inline=True)
        e.add_field(name="🟥  Jackpot Crate",  value="High risk, mega reward",  inline=True)
        await i.response.edit_message(embed=e, view=LuckyBoxView(i.user.id))

    @discord.ui.button(label="❌  Exit", style=discord.ButtonStyle.secondary, row=2)
    async def exit_game(self, i, _):
        await i.response.edit_message(
            embed=e_info("Thanks for playing at Illumix Game Center!", "🎮  See you!"),
            view=None,
        )

# ════════════════════════════════════════════════════════════
#  /coinflip  ─  bet modal → heads/tails → 1.8× payout
# ════════════════════════════════════════════════════════════
COINFLIP_MULTIPLIER = 1.8


class CoinFlipModal(discord.ui.Modal, title="🪙  Coin Flip — Place Your Bet"):
    bet_amount = discord.ui.TextInput(
        label="Bet Amount (pts)", placeholder="e.g. 10",
        min_length=1, max_length=6,
    )

    def __init__(self, uid: int):
        super().__init__()
        self.uid = uid

    async def on_submit(self, i: discord.Interaction):
        try:
            bet = int(self.bet_amount.value.strip())
        except ValueError:
            return await i.response.send_message(
                embed=e_err("Bet must be a whole number."), ephemeral=True)
        if bet <= 0:
            return await i.response.send_message(
                embed=e_err("Bet must be at least 1 pt."), ephemeral=True)

        uid = str(i.user.id)
        users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
        bal = users[uid]["points"]
        if bet > bal:
            return await i.response.send_message(
                embed=e_err(f"You only have `{bal} pts`. Can't bet `{bet}`."),
                ephemeral=True,
            )

        e = _embed(
            "🪙  Coin Flip — Choose Your Side",
            f"**Bet:** `{bet} pts`\n"
            f"**Win:** `{int(bet * COINFLIP_MULTIPLIER)} pts` (1.8×)\n"
            f"**Lose:** `-{bet} pts`\n\n"
            "Pick **Heads** or **Tails**!",
            C.GAME,
        )
        await i.response.send_message(embed=e, view=CoinFlipChoiceView(i.user.id, bet))


class CoinFlipChoiceView(discord.ui.View):
    def __init__(self, uid: int, bet: int):
        super().__init__(timeout=30)
        self.uid = uid
        self.bet = bet

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your coin!"), ephemeral=True)
            return False
        return True

    async def _flip(self, i: discord.Interaction, player_choice: str):
        for c in self.children:
            c.disabled = True

        result   = random.choice(["heads", "tails"])
        coin_em  = "🌕" if result == "heads" else "🌑"
        uid      = str(i.user.id)
        users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})

        if result == player_choice:
            winnings = int(self.bet * COINFLIP_MULTIPLIER)
            profit   = winnings - self.bet
            users[uid]["points"] += profit
            persist_users()
            new_bal = users[uid]["points"]
            e = _embed(
                f"🪙  Coin Flip — {coin_em} {result.capitalize()}  —  🎉  You Win!",
                f"You chose **{player_choice.capitalize()}** — it landed on **{result.capitalize()}**!\n\n"
                f"**Winnings:** `+{profit} pts`\n"
                f"**New Balance:** `{new_bal} pts`",
                C.SUCCESS,
            )
            try:
                dm = e_ok(
                    f"🪙  Coin flip: **{result.capitalize()}** — you won `+{profit} pts`!\n"
                    f"Balance: `{new_bal} pts`",
                    "🎮  Coin Flip Reward",
                )
                await i.user.send(embed=dm)
            except Exception:
                pass
        else:
            users[uid]["points"] -= self.bet
            users[uid]["points"]  = max(0, users[uid]["points"])
            persist_users()
            new_bal = users[uid]["points"]
            e = _embed(
                f"🪙  Coin Flip — {coin_em} {result.capitalize()}  —  💀  You Lose!",
                f"You chose **{player_choice.capitalize()}** — it landed on **{result.capitalize()}**.\n\n"
                f"**Lost:** `-{self.bet} pts`\n"
                f"**New Balance:** `{new_bal} pts`",
                C.ERROR,
            )

        await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="🌕  Heads", style=discord.ButtonStyle.primary,   row=0)
    async def heads(self, i, _): await self._flip(i, "heads")

    @discord.ui.button(label="🌑  Tails", style=discord.ButtonStyle.secondary, row=0)
    async def tails(self, i, _): await self._flip(i, "tails")

    @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel(self, i, _):
        await i.response.edit_message(embed=e_warn("Coin flip cancelled. Bet returned."), view=None)


# ════════════════════════════════════════════════════════════
#  /luck  ─  three mystery boxes with weighted rewards
# ════════════════════════════════════════════════════════════
# Box definitions: (label, emoji, reward_table)
# reward_table = list of (pts, weight) — weights are relative
LUCK_BOXES = {
    "mystery": {
        "label":   "🟦  Mystery Chest",
        "emoji":   "🟦",
        "desc":    "Low risk, steady reward",
        "rewards": [(0, 20), (10, 45), (25, 30), (50, 4), (100, 1)],
    },
    "golden":  {
        "label":   "🟨  Golden Box",
        "emoji":   "🟨",
        "desc":    "Medium risk, bigger potential",
        "rewards": [(0, 35), (10, 25), (25, 20), (50, 15), (100, 5)],
    },
    "jackpot": {
        "label":   "🟥  Jackpot Crate",
        "emoji":   "🟥",
        "desc":    "High risk — mega jackpot chance",
        "rewards": [(0, 55), (10, 15), (25, 10), (50, 10), (100, 10)],
    },
}


def _weighted_reward(rewards: list[tuple[int, int]]) -> int:
    pool    = [pts for pts, w in rewards for _ in range(w)]
    return random.choice(pool)


def _reward_rarity(pts: int) -> str:
    if pts == 0:   return "💀  Nothing"
    if pts == 10:  return "⚪  Common"
    if pts == 25:  return "🔵  Uncommon"
    if pts == 50:  return "🟣  Rare"
    return           "🌟  JACKPOT!"


class LuckyBoxView(discord.ui.View):
    def __init__(self, uid: int):
        super().__init__(timeout=45)
        self.uid = uid

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if i.user.id != self.uid:
            await i.response.send_message(embed=e_err("Not your lucky box!"), ephemeral=True)
            return False
        return True

    async def _open(self, i: discord.Interaction, box_key: str):
        for c in self.children:
            c.disabled = True

        # cooldown enforced here (view-level, after selection)
        remaining = _game_on_cooldown(i.user.id, LUCK_COOLDOWN_SECS)
        if remaining > 0:
            return await i.response.edit_message(
                embed=e_warn(f"⏳  Lucky Box cooldown — wait **{remaining:.0f}s**."),
                view=None,
            )
        game_cooldowns[i.user.id] = time.time()

        box    = LUCK_BOXES[box_key]
        reward = _weighted_reward(box["rewards"])
        rarity = _reward_rarity(reward)

        # suspense animation labels
        suspense = [
            "🎰  Spinning…",
            "🎰  Almost…",
            f"🎰  Opening {box['emoji']}…",
        ]

        if reward > 0:
            new_bal = _award_points(i.user.id, reward)
            color   = C.SUCCESS if reward < 100 else C.PREMIUM
            e = _embed(
                f"{box['emoji']}  {box['label']} — {rarity}",
                f"You opened the **{box['label']}** and found…\n\n"
                f"# `+{reward} pts`\n\n"
                f"**Rarity:** {rarity}\n"
                f"**New Balance:** `{new_bal} pts`",
                color,
            )
            if reward == 100:
                e.description += "\n\n🎆 **JACKPOT! Congratulations!** 🎆"
            try:
                dm = e_ok(
                    f"You opened a **{box['label']}** and won **{reward} pts**!  {rarity}\n"
                    f"Balance: `{new_bal} pts`",
                    "🍀  Lucky Box Reward",
                )
                await i.user.send(embed=dm)
            except Exception:
                pass
        else:
            uid = str(i.user.id)
            bal = users.get(uid, {}).get("points", 0)
            e = _embed(
                f"{box['emoji']}  {box['label']} — {rarity}",
                f"You opened the **{box['label']}** and found…\n\n"
                f"# `Nothing 😢`\n\n"
                f"**Balance:** `{bal} pts`\n"
                f"Better luck next time!",
                C.ERROR,
            )

        e.set_footer(text=f"Illumix Core  •  Cooldown: {LUCK_COOLDOWN_SECS}s")
        await i.response.edit_message(embed=e, view=self)

    @discord.ui.button(label="🟦  Mystery Chest", style=discord.ButtonStyle.primary,   row=0)
    async def mystery(self, i, _): await self._open(i, "mystery")

    @discord.ui.button(label="🟨  Golden Box",    style=discord.ButtonStyle.secondary, row=0)
    async def golden(self,  i, _): await self._open(i, "golden")

    @discord.ui.button(label="🟥  Jackpot Crate", style=discord.ButtonStyle.danger,    row=0)
    async def jackpot(self, i, _): await self._open(i, "jackpot")

    @discord.ui.button(label="🏠  Menu",          style=discord.ButtonStyle.success,   row=1)
    async def back(self, i, _):
        uid = str(i.user.id)
        pts = users.get(uid, {}).get("points", 0)
        await i.response.edit_message(
            embed=_build_game_menu_embed(i.user, pts),
            view=GameMenuView(i.user.id),
        )


# ════════════════════════════════════════════════════════════
#  GIVEAWAY VIEW
# ════════════════════════════════════════════════════════════
class GiveawayView(discord.ui.View):
    def __init__(self, gid: str):
        super().__init__(timeout=None)
        self.gid = gid

    @discord.ui.button(label="🎉  Join Giveaway", style=discord.ButtonStyle.primary,
                       custom_id="join_giveaway")
    async def join(self, i, _):
        ga = giveaways.get(self.gid)
        if not ga or ga["status"] != "active":
            return await i.response.send_message(embed=e_err("Giveaway ended."), ephemeral=True)
        pid = str(i.user.id)
        if pid in ga.get("participants", []):
            return await i.response.send_message(embed=e_err("Already joined."), ephemeral=True)
        ga.setdefault("participants", []).append(pid)
        persist_giveaways()
        e = e_ok(f"Joined! **{len(ga['participants'])}** participants.", "🎉  Joined!")
        await i.response.send_message(embed=e, ephemeral=True)

# ════════════════════════════════════════════════════════════
#  ─────────────────────  COMMANDS  ─────────────────────────
# ════════════════════════════════════════════════════════════

# ── /help ──────────────────────────────────────────────────
@bot.tree.command(name="help", description="📚  Help center — all commands")
async def cmd_help(i: discord.Interaction):
    if not await check_channel(i): return
    embed = _build_help_page("home", i.user)
    view  = HelpView(i.user.id)
    await i.response.send_message(embed=embed, view=view)  # public


# ── /ping ──────────────────────────────────────────────────
@bot.tree.command(name="ping", description="🏓  Bot latency & status")
async def cmd_ping(i: discord.Interaction):
    if not await check_channel(i): return
    t0 = time.monotonic()
    await i.response.defer(ephemeral=True)
    api_ms = round((time.monotonic() - t0) * 1000)
    ws_ms  = round(bot.latency * 1000)
    up     = int(time.time() - BOT_START_TIME)
    h, r   = divmod(up, 3600); m, s = divmod(r, 60)

    if ws_ms < 80:   se, ss = "🟢", "Excellent"
    elif ws_ms < 150: se, ss = "🟡", "Good"
    elif ws_ms < 300: se, ss = "🟠", "Fair"
    else:             se, ss = "🔴", "Poor"

    e = _embed("🏓  Pong!", color=C.SUCCESS)
    e.add_field(name="📡  WebSocket",   value=f"`{ws_ms} ms`",         inline=True)
    e.add_field(name="🌐  API",         value=f"`{api_ms} ms`",        inline=True)
    e.add_field(name=f"{se}  Status",   value=f"`{ss}`",               inline=True)
    e.add_field(name="⏱️  Uptime",      value=f"`{h}h {m}m {s}s`",    inline=True)
    e.add_field(name="🤖  Bot",         value=f"`{bot.user.name}`",    inline=True)
    e.add_field(name="🏠  Guilds",      value=f"`{len(bot.guilds)}`",  inline=True)
    await i.followup.send(embed=e, ephemeral=True)


# ── /deploy ────────────────────────────────────────────────
@bot.tree.command(name="deploy", description="🚀  Deploy a new VPS")
async def cmd_deploy(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    pts = users[uid]["points"]
    if not is_admin(i.user.id) and pts < POINTS_PER_DEPLOY:
        return await i.response.send_message(
            embed=e_err(f"Need `{POINTS_PER_DEPLOY}` pts — you have `{pts}`.\n"
                        "Invite users → `/claimpoint` to earn points."), ephemeral=True)

    e = _embed("🚀  Deploy New VPS",
               f"Your balance: **{pts} pts**  |  Cost: **{POINTS_PER_DEPLOY} pts**\n\n"
               "Select your **Operating System** to continue:",
               C.VPS)
    e.add_field(name="🟠  Ubuntu 22.04 LTS", value="Most compatible, widely used", inline=True)
    e.add_field(name="🔵  Debian 12",         value="Stable, lightweight",          inline=True)
    await i.response.send_message(embed=e, view=DeployView(i.user.id))


# ── /manage ────────────────────────────────────────────────
@bot.tree.command(name="manage", description="⚙️  VPS management wizard")
async def cmd_manage(i: discord.Interaction):
    if not await check_channel(i): return
    uid      = str(i.user.id)
    vps_list = [v for v in vps_db.values()
                if v["owner"] == uid or uid in v.get("shared_with", [])]
    if not vps_list:
        return await i.response.send_message(
            embed=e_err("You have no VPS. Use `/deploy` to create one."), ephemeral=True)
    e = _embed("⚙️  VPS Management",
               "Select a VPS from the dropdown to open its control panel:", C.VPS)
    await i.response.send_message(embed=e, view=ManageSelectView(i.user.id), ephemeral=True)


# ── /port ──────────────────────────────────────────────────
@bot.tree.command(name="port", description="🌐  Port forwarding wizard (Pinggy)")
async def cmd_port(i: discord.Interaction):
    if not await check_channel(i): return
    uid      = str(i.user.id)
    vps_list = [v for v in vps_db.values() if v["owner"] == uid]
    if not vps_list:
        return await i.response.send_message(
            embed=e_err("You have no VPS. Deploy one with `/deploy`."), ephemeral=True)
    e = _embed("🌐  Port Forwarding Wizard",
               "Select your VPS to set up a Pinggy tunnel:\n"
               "*(Credentials will be sent to your DMs)*", C.PREMIUM)
    await i.response.send_message(embed=e, view=PortVPSView(i.user.id), ephemeral=True)


# ── /status ────────────────────────────────────────────────
@bot.tree.command(name="status", description="📊  System health status")
async def cmd_status(i: discord.Interaction):
    if not await check_channel(i): return
    view = StatusView()
    await i.response.send_message(embed=_build_status_embed(), view=view)
    view.msg = await i.original_response()


# ── /list ──────────────────────────────────────────────────
@bot.tree.command(name="list", description="📋  List your VPS")
async def cmd_list(i: discord.Interaction):
    if not await check_channel(i): return
    vps_list = get_user_vps(i.user.id)
    if not vps_list:
        return await i.response.send_message(
            embed=e_err("You have no VPS. Use `/deploy`."), ephemeral=True)
    e = _embed(f"📋  Your VPS — {len(vps_list)} Total", color=C.VPS)
    e.set_thumbnail(url=i.user.display_avatar.url)
    for v in vps_list:
        st    = "🟢 Running" if v["active"] and not v.get("suspended") else (
                "⏸️ Suspended" if v.get("suspended") else "🔴 Stopped")
        exp   = datetime.fromisoformat(v["expires_at"])
        dl    = max(0, (exp - datetime.now(timezone.utc)).days)
        oi    = "🟠" if v.get("os", "ubuntu") == "ubuntu" else "🔵"
        val   = (f"**Status:** {st}  |  **OS:** {oi} `{v.get('os','ubuntu').capitalize()}`\n"
                 f"**HTTP:** `http://{SERVER_IP}:{v['http_port']}`\n"
                 f"**Expires:** `{exp.strftime('%Y-%m-%d')}` ({dl}d)")
        if str(v["owner"]) != str(i.user.id):
            val += "\n**⚠️ Shared VPS**"
        e.add_field(name=f"🖥️  `{v['container_id']}`", value=val, inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /remove ────────────────────────────────────────────────
@bot.tree.command(name="remove", description="🗑️  Remove a VPS (50% refund)")
async def cmd_remove(i: discord.Interaction):
    if not await check_channel(i): return
    uid      = str(i.user.id)
    vps_list = [v for v in vps_db.values() if v["owner"] == uid]
    if not vps_list:
        return await i.response.send_message(
            embed=e_err("You have no VPS to remove."), ephemeral=True)

    async def _vps_picked(interaction: discord.Interaction, cid: str):
        rec    = vps_db.get(cid)
        refund = POINTS_PER_DEPLOY // 2
        warn   = e_warn(
            f"Delete `{cid}`?\n**Refund:** `{refund} pts`\n**This is permanent!**",
            "⚠️  Confirm Remove")
        cv = discord.ui.View(timeout=30)

        @discord.ui.button(label="🗑️  Confirm Remove", style=discord.ButtonStyle.danger)
        async def do_remove_cb(ci: discord.Interaction, __):
            if ci.user.id != interaction.user.id:
                return await ci.response.send_message(embed=e_err("Not yours."), ephemeral=True)
            await ci.response.defer(ephemeral=True)
            await docker_remove(cid)
            if rec and not rec.get("giveaway_vps") and not is_admin(ci.user.id):
                users.setdefault(uid, {"points": 0})
                users[uid]["points"] += refund
                persist_users()
            vps_db.pop(cid, None)
            persist_vps()
            await send_log("VPS Removed", ci.user, cid)
            ee = e_ok(f"`{cid}` removed. `+{refund}` pts refunded.", "🗑️  Removed")
            await ci.followup.send(embed=ee, ephemeral=True)

        async def cancel_cb(ci: discord.Interaction, __):
            await ci.response.send_message(embed=e_ok("Cancelled."), ephemeral=True)

        do_remove = discord.ui.Button(label="🗑️  Confirm Remove", style=discord.ButtonStyle.danger)
        cancel    = discord.ui.Button(label="❌  Cancel",          style=discord.ButtonStyle.secondary)
        do_remove.callback = do_remove_cb
        cancel.callback    = cancel_cb

        cv.add_item(do_remove); cv.add_item(cancel)
        await interaction.response.edit_message(embed=warn, view=cv)

    class RemoveSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(OwnedVPSSelect(i.user.id, _vps_picked, "Select VPS to remove…"))

    e = _embed("🗑️  Remove VPS",
               "Select the VPS you want to delete.\n**50% of deploy cost will be refunded.**",
               C.WARNING)
    await i.response.send_message(embed=e, view=RemoveSelectView(), ephemeral=True)


# ── /admin_create_vps ──────────────────────────────────────
@bot.tree.command(name="admin_create_vps", description="[ADMIN] Create a VPS for a user")
@app_commands.describe(user="The user to create the VPS for")
async def cmd_admin_create_vps(i: discord.Interaction, user: discord.Member):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    e = _embed("🛠️  Admin VPS Wizard",
               f"Creating VPS for **{user.mention}**\n\nStep 1: Select the Operating System",
               C.ADMIN)
    await i.response.send_message(embed=e, view=AdminVPSView(i.user.id, user), ephemeral=False)


# ── /game ──────────────────────────────────────────────────
@bot.tree.command(name="game", description="🎮  Game Center — earn bonus points!")
async def cmd_game(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    pts = users.get(uid, {}).get("points", 0)
    e   = _build_game_menu_embed(i.user, pts)
    await i.response.send_message(embed=e, view=GameMenuView(i.user.id))


# ── /coinflip ──────────────────────────────────────────────
@bot.tree.command(name="coinflip", description="🪙  Bet your points on a coin flip!")
async def cmd_coinflip(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    pts = users[uid]["points"]
    if pts <= 0:
        return await i.response.send_message(
            embed=e_err("You need at least **1 pt** to play coin flip.\nEarn points with `/inv` and `/claimpoint`!"),
            ephemeral=True,
        )
    e = _embed(
        "🪙  Coin Flip",
        f"**Your Balance:** `{pts} pts`\n\n"
        f"Win → **1.8× your bet**\n"
        f"Lose → **lose your bet**\n\n"
        "Press the button to enter your bet amount:",
        C.GAME,
    )
    e.add_field(name="🌕  Heads", value="50% chance", inline=True)
    e.add_field(name="🌑  Tails", value="50% chance", inline=True)

    class QuickBetView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)

        async def interaction_check(self_v, ii: discord.Interaction) -> bool:
            if ii.user.id != i.user.id:
                await ii.response.send_message(embed=e_err("Not your flip!"), ephemeral=True)
                return False
            return True

        @discord.ui.button(label="🪙  Enter Bet & Flip", style=discord.ButtonStyle.primary)
        async def bet_btn(self_v, ii: discord.Interaction, _):
            await ii.response.send_modal(CoinFlipModal(ii.user.id))

        @discord.ui.button(label="❌  Cancel", style=discord.ButtonStyle.secondary)
        async def cancel_btn(self_v, ii: discord.Interaction, _):
            await ii.response.edit_message(embed=e_warn("Coin flip cancelled."), view=None)

    await i.response.send_message(embed=e, view=QuickBetView())


# ── /luck ───────────────────────────────────────────────────
@bot.tree.command(name="luck", description="🍀  Open a mystery box and win points!")
async def cmd_luck(i: discord.Interaction):
    if not await check_channel(i): return
    remaining = _game_on_cooldown(i.user.id, LUCK_COOLDOWN_SECS)
    if remaining > 0:
        return await i.response.send_message(
            embed=e_warn(
                f"⏳  Lucky Box is on cooldown.\nTry again in **{remaining:.0f}s**.",
                "🍀  On Cooldown",
            ),
            ephemeral=True,
        )
    uid = str(i.user.id)
    pts = users.get(uid, {}).get("points", 0)
    e   = _embed(
        "🍀  Lucky Box",
        f"**Your Balance:** `{pts} pts`\n\n"
        "Pick a mystery box — each has different odds!\n"
        "Rewards range from **0 → 100 pts**.",
        C.GAME,
    )
    e.add_field(name="🟦  Mystery Chest", value="Safe pick\n0/10/25/50/100 pts",  inline=True)
    e.add_field(name="🟨  Golden Box",    value="Balanced\n0/10/25/50/100 pts",   inline=True)
    e.add_field(name="🟥  Jackpot Crate", value="High risk\n0 or jackpot chance", inline=True)
    e.set_footer(text=f"Illumix Core  •  Cooldown: {LUCK_COOLDOWN_SECS}s")
    await i.response.send_message(embed=e, view=LuckyBoxView(i.user.id))


# ── /share_vps ─────────────────────────────────────────────
@bot.tree.command(name="share_vps", description="Share VPS access with another user")
@app_commands.describe(container_id="Your VPS ID", user="User to share with")
async def cmd_share_vps(i: discord.Interaction, container_id: str, user: discord.Member):
    if not await check_channel(i): return
    cid = container_id.strip()
    v   = vps_db.get(cid)
    if not v:
        return await i.response.send_message(embed=e_err("VPS not found."), ephemeral=True)
    if v["owner"] != str(i.user.id):
        return await i.response.send_message(embed=e_err("You can only share VPS you own."), ephemeral=True)
    if str(user.id) in v.get("shared_with", []):
        return await i.response.send_message(embed=e_err("Already shared."), ephemeral=True)
    v.setdefault("shared_with", []).append(str(user.id))
    persist_vps()
    await send_log("VPS Shared", i.user, cid, f"With: {user.name}")
    e = e_ok(f"`{cid}` shared with {user.mention}.")
    e.add_field(name="👥  Shared With", value=f"{len(v['shared_with'])} user(s)")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /share_remove ──────────────────────────────────────────
@bot.tree.command(name="share_remove", description="Remove shared VPS access")
@app_commands.describe(container_id="VPS ID", user="User to remove")
async def cmd_share_remove(i: discord.Interaction, container_id: str, user: discord.Member):
    if not await check_channel(i): return
    cid = container_id.strip()
    v   = vps_db.get(cid)
    if not v:
        return await i.response.send_message(embed=e_err("VPS not found."), ephemeral=True)
    if v["owner"] != str(i.user.id) and not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("No permission."), ephemeral=True)
    if str(user.id) not in v.get("shared_with", []):
        return await i.response.send_message(embed=e_err("Not shared with that user."), ephemeral=True)
    v["shared_with"].remove(str(user.id))
    persist_vps()
    await i.response.send_message(embed=e_ok(f"Removed access from {user.mention}."), ephemeral=True)


# ── /suspend ───────────────────────────────────────────────
@bot.tree.command(name="suspend", description="[ADMIN] Suspend a VPS")
async def cmd_suspend(i: discord.Interaction):
    if not await check_channel(i):
        return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)

    active_vps = [v for v in vps_db.values() if not v.get("suspended")]
    if not active_vps:
        return await i.response.send_message(
            embed=e_warn("No active VPS to suspend."), ephemeral=True
        )

    # ── Callback when a VPS is selected ─────────────────────────
    async def _vps_picked(interaction: discord.Interaction, cid: str):
        v = vps_db.get(cid)
        if not v:
            return await interaction.response.send_message(embed=e_err("VPS not found."), ephemeral=True)
        if v.get("suspended"):
            return await interaction.response.send_message(embed=e_warn("Already suspended."), ephemeral=True)

        os_label = "Ubuntu 22.04" if v.get("os", "ubuntu") == "ubuntu" else "Debian 12"
        confirm_embed = _embed(
            "⏸️  Confirm Suspend",
            f"Suspend container `{cid}`?\nThe container will be **stopped** immediately.",
            C.WARNING
        )
        confirm_embed.add_field(name="🆔  Container", value=f"`{cid}`",                       inline=True)
        confirm_embed.add_field(name="👤  Owner",     value=f"`{v['owner']}`",                inline=True)
        confirm_embed.add_field(name="🖥️  OS",        value=f"`{os_label}`",                  inline=True)
        confirm_embed.add_field(name="💻  Specs",     value=f"`{v['ram']}GB · {v['cpu']}CPU`", inline=True)
        confirm_embed.add_field(name="⏰  Expires",   value=f"`{v['expires_at'][:10]}`",       inline=True)

        cv = discord.ui.View(timeout=30)

        # ── Confirm Suspend Button ───────────────────────────────
        async def do_suspend_cb(ci: discord.Interaction):
            await ci.response.defer(ephemeral=True)
            if await docker_stop(cid):
                v["active"] = False
                v["suspended"] = True
                persist_vps()
                await send_log("VPS Suspended", ci.user, cid, "Admin wizard")
                await ci.followup.send(embed=e_ok(f"`{cid}` has been suspended."), ephemeral=True)
            else:
                await ci.followup.send(embed=e_err("Failed to stop the container."), ephemeral=True)

        # ── Cancel Suspend Button ───────────────────────────────
        async def cancel_suspend_cb(ci: discord.Interaction):
            await ci.response.edit_message(embed=e_ok("Suspension cancelled."), view=None)

        do_suspend     = discord.ui.Button(label="⏸️  Confirm Suspend", style=discord.ButtonStyle.danger)
        cancel_suspend = discord.ui.Button(label="❌  Cancel",           style=discord.ButtonStyle.secondary)

        do_suspend.callback     = do_suspend_cb
        cancel_suspend.callback = cancel_suspend_cb

        cv.add_item(do_suspend)
        cv.add_item(cancel_suspend)

        await interaction.response.edit_message(embed=confirm_embed, view=cv)

    # ── Dropdown view for selecting VPS ───────────────────────
    class SuspendSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(AllVPSSelect(
                _vps_picked,
                placeholder="⏸️  Select a VPS to suspend…",
                filter_fn=lambda v: not v.get("suspended"),
            ))

    # ── Intro embed ─────────────────────────────────────────
    intro = _embed(
        "⏸️  Suspend VPS — Admin Wizard",
        "Select the VPS you want to suspend from the dropdown.\n"
        "Only **active** (non-suspended) containers are shown.",
        C.ADMIN
    )
    intro.add_field(name="⚠️  Effect", value="Container will be stopped until unsuspended.", inline=False)

    await i.response.send_message(embed=intro, view=SuspendSelectView(), ephemeral=True)
# ── /unsuspend ─────────────────────────────────────────────
@bot.tree.command(name="unsuspend", description="[ADMIN] Unsuspend a VPS")
@app_commands.describe(container_id="Container ID")
async def cmd_unsuspend(i: discord.Interaction, container_id: str):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    cid = container_id.strip()
    v   = vps_db.get(cid)
    if not v: return await i.response.send_message(embed=e_err("VPS not found."), ephemeral=True)
    if not v.get("suspended"):
        return await i.response.send_message(embed=e_warn("Not suspended."), ephemeral=True)
    await i.response.defer(ephemeral=True)
    if await docker_start(cid):
        v["active"] = True; v["suspended"] = False; persist_vps()
        await send_log("VPS Unsuspended", i.user, cid)
        await i.followup.send(embed=e_ok(f"`{cid}` unsuspended."), ephemeral=True)
    else:
        await i.followup.send(embed=e_err("Failed to unsuspend."), ephemeral=True)


# ── /delete_vps ────────────────────────────────────────────
@bot.tree.command(name="delete_vps", description="[ADMIN] Force-delete any VPS and remove it from the database")
async def cmd_delete_vps(i: discord.Interaction):
    if not await check_channel(i):
        return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)

    if not vps_db:
        return await i.response.send_message(
            embed=e_warn("No VPS exist in the database."), ephemeral=True
        )

    # ── Callback when a VPS is selected ───────────────────────
    async def _vps_picked(interaction: discord.Interaction, cid: str):
        v = vps_db.get(cid)
        if not v:
            return await interaction.response.send_message(embed=e_err("VPS not found."), ephemeral=True)

        os_label = "Ubuntu 22.04" if v.get("os", "ubuntu") == "ubuntu" else "Debian 12"
        confirm_embed = _embed(
            "🗑️  Confirm Force-Delete",
            f"**Permanently delete** container `{cid}`?\n"
            "This will stop the container, remove it from Docker, and wipe it from the database.\n"
            "⚠️  **This cannot be undone.**",
            C.ERROR
        )
        confirm_embed.add_field(name="🆔  Container", value=f"`{cid}`",                        inline=True)
        confirm_embed.add_field(name="👤  Owner",     value=f"`{v['owner']}`",                 inline=True)
        confirm_embed.add_field(name="🖥️  OS",        value=f"`{os_label}`",                   inline=True)
        confirm_embed.add_field(name="💻  Specs",     value=f"`{v['ram']}GB · {v['cpu']}CPU`", inline=True)
        confirm_embed.add_field(name="⏰  Expires",   value=f"`{v['expires_at'][:10]}`",        inline=True)
        st = "⏸️ Suspended" if v.get("suspended") else ("🟢 Active" if v["active"] else "🔴 Stopped")
        confirm_embed.add_field(name="📊  Status",    value=st,                                inline=True)

        cv = discord.ui.View(timeout=30)

        # ── Confirm Delete Button ─────────────────────────────
        async def do_delete_cb(ci: discord.Interaction):
            await ci.response.defer(ephemeral=True)
            await docker_stop(cid)
            await docker_remove(cid)
            owner_id = v.get("owner", "unknown")
            vps_db.pop(cid, None)
            persist_vps()
            await send_log("VPS Force-Deleted", ci.user, cid, f"Owner UID: {owner_id}")
            done = e_ok(
                f"Container `{cid}` has been stopped, removed from Docker, and wiped from the database.",
                title="🗑️  VPS Deleted"
            )
            done.add_field(name="🆔  Container", value=f"`{cid}`",      inline=True)
            done.add_field(name="👤  Owner",     value=f"`{owner_id}`", inline=True)
            await ci.followup.send(embed=done, ephemeral=True)

        # ── Cancel Delete Button ─────────────────────────────
        async def cancel_delete_cb(ci: discord.Interaction):
            await ci.response.edit_message(embed=e_ok("Deletion cancelled."), view=None)

        do_delete     = discord.ui.Button(label="🗑️  Confirm Delete", style=discord.ButtonStyle.danger)
        cancel_delete = discord.ui.Button(label="❌  Cancel",          style=discord.ButtonStyle.secondary)

        do_delete.callback     = do_delete_cb
        cancel_delete.callback = cancel_delete_cb

        cv.add_item(do_delete)
        cv.add_item(cancel_delete)

        await interaction.response.edit_message(embed=confirm_embed, view=cv)

    # ── Dropdown view for selecting VPS ───────────────────────
    class DeleteSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(AllVPSSelect(
                _vps_picked,
                placeholder="🗑️  Select a VPS to delete…",
            ))

    # ── Intro embed ─────────────────────────────────────────
    intro = _embed(
        "🗑️  Force-Delete VPS — Admin Wizard",
        "Select the VPS you want to **permanently delete** from the dropdown.\n"
        "All containers (active, suspended, stopped) are shown.",
        C.ADMIN
    )
    intro.add_field(name="⚠️  Warning", value="Deletion is **permanent** and cannot be reversed.", inline=False)

    await i.response.send_message(embed=intro, view=DeleteSelectView(), ephemeral=True)

# ── /unsuspend_vps ─────────────────────────────────────────
@bot.tree.command(name="unsuspend_vps", description="[ADMIN] Unsuspend a VPS and notify its owner via DM")
async def cmd_unsuspend_vps(i: discord.Interaction):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)

    suspended_vps = [v for v in vps_db.values() if v.get("suspended")]
    if not suspended_vps:
        return await i.response.send_message(
            embed=e_warn("No suspended VPS to unsuspend."), ephemeral=True)

    # ── Callback when a VPS is selected ───────────────────────
    async def _vps_picked(interaction: discord.Interaction, cid: str):
        v = vps_db.get(cid)
        if not v:
            return await interaction.response.send_message(embed=e_err("VPS not found."), ephemeral=True)
        if not v.get("suspended"):
            return await interaction.response.send_message(embed=e_warn("VPS is not suspended."), ephemeral=True)

        os_label = "Ubuntu 22.04" if v.get("os", "ubuntu") == "ubuntu" else "Debian 12"
        confirm_embed = _embed(
            "✅  Confirm Unsuspend",
            f"Unsuspend container `{cid}` and bring it back online?",
            C.SUCCESS
        )
        confirm_embed.add_field(name="🆔  Container", value=f"`{cid}`",                        inline=True)
        confirm_embed.add_field(name="👤  Owner",     value=f"`{v['owner']}`",                 inline=True)
        confirm_embed.add_field(name="🖥️  OS",        value=f"`{os_label}`",                   inline=True)
        confirm_embed.add_field(name="💻  Specs",     value=f"`{v['ram']}GB · {v['cpu']}CPU`", inline=True)
        confirm_embed.add_field(name="⏰  Expires",   value=f"`{v['expires_at'][:10]}`",        inline=True)
        confirm_embed.add_field(name="📬  Owner DM",  value="Owner will be notified via DM",   inline=True)

        cv = discord.ui.View(timeout=30)

        # ── Confirm Unsuspend Button ───────────────────────────
        async def do_unsuspend_cb(ci: discord.Interaction):
            await ci.response.defer(ephemeral=True)
            if await docker_start(cid):
                v["active"] = True
                v["suspended"] = False
                persist_vps()
                await send_log("VPS Unsuspended", ci.user, cid)
                try:
                    owner_user = await bot.fetch_user(int(v["owner"]))
                    dm = _embed(
                        "✅  VPS Unsuspended",
                        f"Your VPS `{cid}` has been unsuspended by an admin and is now active again.",
                        C.SUCCESS
                    )
                    dm.add_field(name="🆔  Container", value=f"`{cid}`",               inline=True)
                    dm.add_field(name="⏰  Expires",   value=f"`{v['expires_at'][:10]}`", inline=True)
                    await owner_user.send(embed=dm)
                except Exception:
                    pass
                await ci.followup.send(
                    embed=e_ok(f"`{cid}` unsuspended. Owner notified via DM."), ephemeral=True
                )
            else:
                await ci.followup.send(embed=e_err("Failed to start the container."), ephemeral=True)

        # ── Cancel Unsuspend Button ───────────────────────────
        async def cancel_unsuspend_cb(ci: discord.Interaction):
            await ci.response.edit_message(embed=e_ok("Unsuspend cancelled."), view=None)

        do_unsuspend     = discord.ui.Button(label="✅  Confirm Unsuspend", style=discord.ButtonStyle.success)
        cancel_unsuspend = discord.ui.Button(label="❌  Cancel",             style=discord.ButtonStyle.secondary)

        do_unsuspend.callback     = do_unsuspend_cb
        cancel_unsuspend.callback = cancel_unsuspend_cb

        cv.add_item(do_unsuspend)
        cv.add_item(cancel_unsuspend)

        await interaction.response.edit_message(embed=confirm_embed, view=cv)

    # ── Dropdown view for selecting suspended VPS ─────────────
    class UnsuspendSelectView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(AllVPSSelect(
                _vps_picked,
                placeholder="✅  Select a VPS to unsuspend…",
                filter_fn=lambda v: v.get("suspended"),
            ))

    # ── Intro embed ─────────────────────────────────────────
    intro = _embed(
        "✅  Unsuspend VPS — Admin Wizard",
        "Select the VPS you want to unsuspend from the dropdown.\n"
        "Only **suspended** containers are shown.",
        C.ADMIN
    )
    intro.add_field(name="📬  Note", value="The VPS owner will receive a DM notification.", inline=False)

    await i.response.send_message(embed=intro, view=UnsuspendSelectView(), ephemeral=True)

# ── /listsall ──────────────────────────────────────────────
@bot.tree.command(name="listsall", description="[ADMIN] List all VPS")
async def cmd_listsall(i: discord.Interaction):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if not vps_db:
        return await i.response.send_message(embed=e_info("No VPS found.", "📋  All VPS"), ephemeral=True)
    e = _embed(f"📋  All VPS — {len(vps_db)} Total", color=C.ADMIN)
    for cid, v in list(vps_db.items())[:10]:
        try:
            owner_user = await bot.fetch_user(int(v["owner"]))
            on = owner_user.name
        except Exception:
            on = f"User {v['owner']}"
        st  = "🟢" if v["active"] and not v.get("suspended") else ("⏸️" if v.get("suspended") else "🔴")
        oi  = "🟠" if v.get("os", "ubuntu") == "ubuntu" else "🔵"
        dl  = max(0, (datetime.fromisoformat(v["expires_at"]) - datetime.now(timezone.utc)).days)
        e.add_field(
            name=f"`{cid}`",
            value=(f"**Owner:** {on}  {st} {oi}\n"
                   f"**Specs:** `{v['ram']}GB · {v['cpu']}CPU · {v['disk']}GB`\n"
                   f"**Exp:** `{v['expires_at'][:10]}` ({dl}d)"),
            inline=False)
    if len(vps_db) > 10:
        e.set_footer(text=f"Showing 10 of {len(vps_db)} VPS  •  Illumix Core")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /mass_port ─────────────────────────────────────────────
@bot.tree.command(name="mass_port", description="[ADMIN] Add a port to multiple VPS")
@app_commands.describe(port="Port number", container_ids="Comma-separated container IDs")
async def cmd_mass_port(i: discord.Interaction, port: int, container_ids: str):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if not 1 <= port <= 65535:
        return await i.response.send_message(embed=e_err("Port must be 1–65535."), ephemeral=True)
    await i.response.defer(ephemeral=True)
    ok_l, fail_l = [], []
    for cid in [c.strip() for c in container_ids.split(",")]:
        if cid in vps_db:
            v = vps_db[cid]
            v.setdefault("additional_ports", [])
            if port not in v["additional_ports"]:
                v["additional_ports"].append(port)
                ok_l.append(cid)
            else:
                fail_l.append(f"{cid} (already has)")
        else:
            fail_l.append(f"{cid} (not found)")
    persist_vps()
    e = _embed(f"🔌  Mass Port {port}", color=C.ADMIN)
    e.add_field(name=f"✅  Success ({len(ok_l)})",  value="\n".join(ok_l[:10])   or "None", inline=False)
    e.add_field(name=f"❌  Failed ({len(fail_l)})",  value="\n".join(fail_l[:10]) or "None", inline=False)
    await i.followup.send(embed=e, ephemeral=True)


# ── /pointbal ──────────────────────────────────────────────
@bot.tree.command(name="pointbal", description="💳  Check your points balance")
async def cmd_pointbal(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    u   = users[uid]
    pts = u["points"]
    pct = min((pts / POINTS_PER_DEPLOY) * 100, 100) if POINTS_PER_DEPLOY else 0
    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    e   = _embed("💰  Points Balance", color=C.PREMIUM)
    e.set_thumbnail(url=i.user.display_avatar.url)
    e.add_field(name="💳  Balance",          value=f"`{pts} pts`",             inline=True)
    e.add_field(name="🎁  Unclaimed Invites", value=f"`{u['inv_unclaimed']}`", inline=True)
    e.add_field(name="📈  Total Earned",      value=f"`{u['inv_total']}`",     inline=True)
    e.add_field(name="🚀  Deploy Progress",
                value=f"```{bar} {pct:.0f}%\n{pts}/{POINTS_PER_DEPLOY} pts```", inline=False)
    e.add_field(name="Status",
                value="✅  Ready to `/deploy`!" if pts >= POINTS_PER_DEPLOY
                else f"❌  Need `{POINTS_PER_DEPLOY - pts}` more pts",
                inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /inv ───────────────────────────────────────────────────
@bot.tree.command(name="inv", description="📊  Your invite stats & progress")
async def cmd_inv(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0, "unique_joins": []})
    u   = users[uid]
    pts = u["points"]
    pct = min((pts / POINTS_PER_DEPLOY) * 100, 100) if POINTS_PER_DEPLOY else 0
    prog = "🟩" * int(pct / 20) + "⬛" * (5 - int(pct / 20))
    e    = _embed("📊  Invites & Points Dashboard", color=C.GIVEAWAY)
    e.set_thumbnail(url=i.user.display_avatar.url)
    e.add_field(name="💰  Points",
                value=f"```Current:  {pts}\nUnclaimed:{u['inv_unclaimed']}\nTotal inv:{len(u.get('unique_joins',[]))}```",
                inline=False)
    e.add_field(name="📈  Progress",
                value=f"```{prog} {pct:.0f}%\n{pts}/{POINTS_PER_DEPLOY} pts```", inline=False)
    if u.get("inv_unclaimed", 0) > 0:
        e.add_field(name="⚡  Quick Action",
                    value=f"Use `/claimpoint` to convert **{u['inv_unclaimed']} invites** → pts!",
                    inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /claimpoint ────────────────────────────────────────────
@bot.tree.command(name="claimpoint", description="🎁  Convert invites to points")
async def cmd_claimpoint(i: discord.Interaction):
    if not await check_channel(i): return
    uid = str(i.user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    u   = users[uid]
    if u["inv_unclaimed"] <= 0:
        return await i.response.send_message(
            embed=e_warn("No unclaimed invites. Invite new users first!", "❌  Nothing to Claim"),
            ephemeral=True)
    claimed = u["inv_unclaimed"]
    old_pts = u["points"]
    u["points"] += claimed
    u["inv_unclaimed"] = 0
    persist_users()
    e = e_ok(f"Converted **{claimed} invites** → **{claimed} pts**!", "✅  Points Claimed!")
    e.add_field(name="Before", value=f"`{old_pts} pts`",    inline=True)
    e.add_field(name="After",  value=f"`{u['points']} pts`", inline=True)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /point_share ───────────────────────────────────────────
@bot.tree.command(name="point_share", description="💸  Send points to another user")
@app_commands.describe(amount="Amount", user="Recipient")
async def cmd_point_share(i: discord.Interaction, amount: int, user: discord.Member):
    if not await check_channel(i): return
    if amount <= 0:
        return await i.response.send_message(embed=e_err("Amount must be > 0."), ephemeral=True)
    if user.id == i.user.id:
        return await i.response.send_message(embed=e_err("Can't share with yourself."), ephemeral=True)
    sid = str(i.user.id); rid = str(user.id)
    users.setdefault(sid, {"points": 0})
    if users[sid]["points"] < amount:
        return await i.response.send_message(embed=e_err("Insufficient points."), ephemeral=True)
    users[sid]["points"] -= amount
    users.setdefault(rid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    users[rid]["points"] += amount
    persist_users()
    e = e_ok(f"Sent **{amount} pts** to {user.mention}.", "💸  Points Sent")
    e.add_field(name="Your Balance", value=f"`{users[sid]['points']} pts`")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /pointtop ──────────────────────────────────────────────
@bot.tree.command(name="pointtop", description="🏆  Points leaderboard")
async def cmd_pointtop(i: discord.Interaction):
    if not await check_channel(i): return
    top    = sorted(users.items(), key=lambda x: x[1].get("points", 0), reverse=True)[:10]
    medals = ["🥇","🥈","🥉"] + ["🏅"]*7
    e      = _embed("🏆  Points Leaderboard", color=C.PREMIUM)
    for idx, (uid, ud) in enumerate(top):
        try:
            u = await bot.fetch_user(int(uid))
            name = u.name
        except Exception:
            name = f"User {uid}"
        e.add_field(name=f"{medals[idx]} #{idx+1} {name}", value=f"`{ud.get('points',0)} pts`", inline=True)
    await i.response.send_message(embed=e)


# ── /pointgive ─────────────────────────────────────────────
@bot.tree.command(name="pointgive", description="[ADMIN] Give points to a user")
@app_commands.describe(amount="Amount", user="Target")
async def cmd_pointgive(i: discord.Interaction, amount: int, user: discord.Member):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if amount <= 0:
        return await i.response.send_message(embed=e_err("Amount must be > 0."), ephemeral=True)
    uid = str(user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    users[uid]["points"] += amount; persist_users()
    e = e_ok(f"Gave **{amount} pts** to {user.mention}.", "💰  Points Given")
    e.add_field(name="New Balance", value=f"`{users[uid]['points']} pts`")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /pointremove ───────────────────────────────────────────
@bot.tree.command(name="pointremove", description="[ADMIN] Remove points from a user")
@app_commands.describe(amount="Amount", user="Target")
async def cmd_pointremove(i: discord.Interaction, amount: int, user: discord.Member):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if amount <= 0:
        return await i.response.send_message(embed=e_err("Amount must be > 0."), ephemeral=True)
    uid = str(user.id)
    users.setdefault(uid, {"points": 0, "inv_unclaimed": 0, "inv_total": 0})
    actual = min(amount, users[uid]["points"])
    users[uid]["points"] -= actual; persist_users()
    e = e_ok(f"Removed **{actual} pts** from {user.mention}.", "💸  Points Removed")
    e.add_field(name="New Balance", value=f"`{users[uid]['points']} pts`")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /pointlistall ──────────────────────────────────────────
@bot.tree.command(name="pointlistall", description="[ADMIN] All users with points")
async def cmd_pointlistall(i: discord.Interaction):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    with_pts = sorted([(uid, d) for uid, d in users.items() if d.get("points", 0) > 0],
                      key=lambda x: x[1]["points"], reverse=True)
    if not with_pts:
        return await i.response.send_message(embed=e_info("No users with points.", "📊  Points"), ephemeral=True)
    e = _embed(f"📊  All Users with Points — {len(with_pts)}", color=C.INFO)
    for uid, ud in with_pts[:15]:
        try:
            u = await bot.fetch_user(int(uid))
            name = u.name
        except Exception:
            name = f"User {uid}"
        e.add_field(name=name, value=f"`{ud['points']} pts`", inline=True)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /admin_add ─────────────────────────────────────────────
@bot.tree.command(name="admin_add", description="[MAIN ADMIN] Add an admin")
@app_commands.describe(user="User to promote")
async def cmd_admin_add(i: discord.Interaction, user: discord.Member):
    if not await check_channel(i): return
    if i.user.id not in MAIN_ADMIN_IDS and i.user.id != OWNER_ID:
        return await i.response.send_message(embed=e_err("Main admin only."), ephemeral=True)
    if user.id in ADMIN_IDS:
        return await i.response.send_message(embed=e_warn("Already an admin."), ephemeral=True)
    af = os.path.join(DATA_DIR, "admins.json")
    al = load_json(af, []); al.append(user.id); save_json(af, al)
    ADMIN_IDS.add(user.id)
    await send_log("Admin Added", i.user, details=f"Added: {user.name}")
    e = e_ok(f"**{user.name}** is now an admin.", "🛡️  Admin Added")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /admin_remove ──────────────────────────────────────────
@bot.tree.command(name="admin_remove", description="[MAIN ADMIN] Remove an admin")
@app_commands.describe(user="User to demote")
async def cmd_admin_remove(i: discord.Interaction, user: discord.Member):
    if not await check_channel(i): return
    if i.user.id not in MAIN_ADMIN_IDS and i.user.id != OWNER_ID:
        return await i.response.send_message(embed=e_err("Main admin only."), ephemeral=True)
    if user.id in MAIN_ADMIN_IDS or user.id == OWNER_ID:
        return await i.response.send_message(embed=e_err("Cannot remove core admin/owner."), ephemeral=True)
    af = os.path.join(DATA_DIR, "admins.json")
    al = load_json(af, [])
    if user.id not in al:
        return await i.response.send_message(embed=e_warn(f"{user.name} is not an admin."), ephemeral=True)
    al.remove(user.id); save_json(af, al)
    ADMIN_IDS.discard(user.id)
    await send_log("Admin Removed", i.user, details=f"Removed: {user.name}")
    await i.response.send_message(embed=e_ok(f"**{user.name}** removed from admin."), ephemeral=True)


# ── /admins ────────────────────────────────────────────────
@bot.tree.command(name="admins", description="🛡️  List all admins")
async def cmd_admins(i: discord.Interaction):
    if not await check_channel(i): return
    af   = os.path.join(DATA_DIR, "admins.json")
    xtra = load_json(af, [])
    e    = _embed("🛡️  Admin Users", color=C.ADMIN)
    try:
        ow = await bot.fetch_user(OWNER_ID)
        e.add_field(name="👑  Owner", value=ow.mention, inline=False)
    except Exception:
        e.add_field(name="👑  Owner", value=f"`{OWNER_ID}`", inline=False)
    mains = []
    for aid in MAIN_ADMIN_IDS:
        try:
            u = await bot.fetch_user(aid); mains.append(u.mention)
        except Exception:
            mains.append(f"`{aid}`")
    if mains: e.add_field(name="🔐  Main Admins", value="\n".join(mains), inline=False)
    extras = []
    for aid in xtra:
        try:
            u = await bot.fetch_user(aid); extras.append(u.mention)
        except Exception:
            extras.append(f"`{aid}`")
    e.add_field(name="📋  Additional Admins", value="\n".join(extras) if extras else "None", inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /set_log_channel ───────────────────────────────────────
@bot.tree.command(name="set_log_channel", description="[ADMIN] Set log channel")
@app_commands.describe(channel="Channel for logs")
async def cmd_set_log(i: discord.Interaction, channel: discord.TextChannel):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    global LOG_CHANNEL_ID
    LOG_CHANNEL_ID = channel.id
    cfg = load_json(os.path.join(DATA_DIR, "config.json"), {})
    cfg["log_channel_id"] = channel.id
    save_json(os.path.join(DATA_DIR, "config.json"), cfg)
    e = e_ok(f"Log channel set to {channel.mention}.", "📊  Log Channel Set")
    await i.response.send_message(embed=e, ephemeral=True)


# ── /logs ──────────────────────────────────────────────────
@bot.tree.command(name="logs", description="[ADMIN] View recent activity logs")
@app_commands.describe(limit="Lines to show (1–25, default 10)")
async def cmd_logs(i: discord.Interaction, limit: int = 10):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    limit     = max(1, min(25, limit))
    logs_data = load_json(os.path.join(DATA_DIR, "vps_logs.json"), [])
    if not logs_data:
        return await i.response.send_message(embed=e_info("No logs yet.", "📊  Logs"), ephemeral=True)
    recent = list(reversed(logs_data[-limit:]))
    e      = _embed(f"📊  Logs — Last {len(recent)}", color=C.INFO)
    for idx, log in enumerate(recent, 1):
        try:
            td = f"<t:{int(datetime.fromisoformat(log['ts']).timestamp())}:R>"
        except Exception:
            td = "Recently"
        val = f"👤 `{log.get('user','?')}` • {td}"
        if log.get("details"): val += f"\n📝 `{log['details'][:80]}`"
        if log.get("vps_id"):  val += f"\n🆔 `{log['vps_id']}`"
        e.add_field(name=f"`{idx}.` {log.get('action','?')}", value=val, inline=False)
    await i.response.send_message(embed=e, ephemeral=True)


# ── /giveaway_create ───────────────────────────────────────
@bot.tree.command(name="giveaway_create", description="[ADMIN] Create a VPS giveaway")
@app_commands.describe(
    duration_minutes="Duration (minutes)", vps_ram="RAM GB",
    vps_cpu="CPU cores",                   vps_disk="Disk GB",
    winner_type="random or all",           description="Description",
)
async def cmd_giveaway_create(i: discord.Interaction,
                               duration_minutes: int, vps_ram: int, vps_cpu: int,
                               vps_disk: int, winner_type: str,
                               description: str = "VPS Giveaway"):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if winner_type not in ["random", "all"]:
        return await i.response.send_message(embed=e_err("winner_type: random or all"), ephemeral=True)
    if duration_minutes < 1:
        return await i.response.send_message(embed=e_err("Duration ≥ 1 min."), ephemeral=True)
    gid      = f"giveaway_{secrets.token_hex(3)}"
    end_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
    giveaways[gid] = {
        "id": gid, "creator_id": str(i.user.id), "description": description,
        "vps_ram": vps_ram, "vps_cpu": vps_cpu, "vps_disk": vps_disk,
        "winner_type": winner_type, "end_time": end_time.isoformat(),
        "status": "active", "participants": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    persist_giveaways()
    e = _embed("🎉  VPS Giveaway!", color=C.GIVEAWAY)
    e.set_author(name=f"By {i.user.name}", icon_url=i.user.display_avatar.url)
    e.add_field(name="📋  Description", value=description, inline=False)
    e.add_field(name="💻  Prize",       value=f"`{vps_ram}GB · {vps_cpu} CPU · {vps_disk}GB`", inline=False)
    e.add_field(name="🏆  Winner",      value=winner_type.capitalize(), inline=True)
    e.add_field(name="⏰  Ends",        value=f"<t:{int(end_time.timestamp())}:R>", inline=True)
    await i.response.send_message(embed=e, view=GiveawayView(gid))


# ── /giveaway_list ─────────────────────────────────────────
@bot.tree.command(name="giveaway_list", description="[ADMIN] List all giveaways")
async def cmd_giveaway_list(i: discord.Interaction):
    if not await check_channel(i): return
    if not is_admin(i.user.id):
        return await i.response.send_message(embed=e_err("Admin only."), ephemeral=True)
    if not giveaways:
        return await i.response.send_message(embed=e_info("No giveaways.", "🎉  Giveaways"), ephemeral=True)
    e = _embed(f"🎉  Giveaways — {len(giveaways)}", color=C.GIVEAWAY)
    for g in list(giveaways.values())[:8]:
        end  = datetime.fromisoformat(g["end_time"])
        stat = "🟢" if g["status"] == "active" else "⚫"
        e.add_field(
            name=f"{stat} `{g['id']}`",
            value=(f"**Prize:** `{g['vps_ram']}GB/{g['vps_cpu']}CPU`  "
                   f"**Participants:** {len(g.get('participants',[]))}\n"
                   f"**Ends:** <t:{int(end.timestamp())}:R>"),
            inline=False)
    await i.response.send_message(embed=e, ephemeral=True)

# ════════════════════════════════════════════════════════════
#  BACKGROUND TASKS
# ════════════════════════════════════════════════════════════
@tasks.loop(minutes=10)
async def expire_loop():
    now = datetime.now(timezone.utc)
    for cid, rec in list(vps_db.items()):
        try:
            if not rec.get("active"):
                continue

            # Ensure expires_at is UTC-aware
            expires_at = datetime.fromisoformat(rec["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)

            if now >= expires_at:
                await docker_stop(cid)
                rec["active"] = False
                rec["suspended"] = True
                persist_vps()

                try:
                    u = await bot.fetch_user(int(rec["owner"]))
                    e = e_warn(
                        f"Your VPS `{cid}` expired and was suspended.\n"
                        f"Use `/manage` → **⏳ Renew** to reactivate.",
                        "⏸️ VPS Expired"
                    )
                    await u.send(embed=e)
                except Exception as user_exc:
                    logger.error(f"Failed to DM user {rec['owner']} for expired VPS {cid}: {user_exc}")

                await send_log("VPS Expired", rec["owner"], cid, "Auto-suspended")
        except Exception as loop_exc:
            logger.error(f"Error processing VPS {cid} in expire_loop: {loop_exc}")


@tasks.loop(minutes=5)
async def giveaway_loop():
    now = datetime.now(timezone.utc)
    for gid, ga in list(giveaways.items()):
        try:
            # Skip inactive or not yet ended giveaways
            end_time = datetime.fromisoformat(ga["end_time"])
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)

            if ga["status"] != "active" or now < end_time:
                continue

            participants = ga.get("participants", [])
            if not participants:
                ga["status"] = "ended"
                persist_giveaways()
                continue

            # Random winner giveaway
            if ga.get("winner_type") == "random":
                winner_id = random.choice(participants)
                ga["winner_id"] = winner_id
                ga["status"] = "ended"

                try:
                    rec = await create_vps(
                        int(winner_id),
                        ga["vps_ram"],
                        ga["vps_cpu"],
                        ga["vps_disk"],
                        giveaway=True
                    )
                    if "error" not in rec:
                        ga["vps_created"] = True
                        try:
                            winner_user = await bot.fetch_user(int(winner_id))
                            e = _embed("🎉 You Won a VPS!", color=C.GIVEAWAY)
                            e.add_field(name="🆔 Container", value=f"`{rec['container_id']}`", inline=False)
                            e.add_field(name="👤 Username", value=f"```{rec['vps_user']}```", inline=True)
                            e.add_field(name="🔑 Password", value=f"```{rec['vps_pass']}```", inline=True)
                            e.add_field(name="🔗 SSH", value=f"```{rec['ssh']}```", inline=False)
                            await winner_user.send(embed=e)
                        except Exception as dm_exc:
                            logger.error(f"Failed to DM winner {winner_id}: {dm_exc}")
                except Exception as vps_exc:
                    logger.error(f"Giveaway VPS creation error for winner {winner_id}: {vps_exc}")

            # All participants get VPS (non-random)
            else:
                count = 0
                for pid in participants:
                    try:
                        rec = await create_vps(
                            int(pid),
                            ga["vps_ram"],
                            ga["vps_cpu"],
                            ga["vps_disk"],
                            giveaway=True
                        )
                        if "error" not in rec:
                            count += 1
                            try:
                                p_user = await bot.fetch_user(int(pid))
                                e = _embed("🎉 You Got a VPS!", color=C.GIVEAWAY)
                                e.add_field(name="🆔 Container", value=f"`{rec['container_id']}`", inline=False)
                                e.add_field(name="👤 Username", value=f"```{rec['vps_user']}```", inline=True)
                                e.add_field(name="🔑 Password", value=f"```{rec['vps_pass']}```", inline=True)
                                e.add_field(name="🔗 SSH", value=f"```{rec['ssh']}```", inline=False)
                                await p_user.send(embed=e)
                            except Exception as dm_exc:
                                logger.error(f"Failed to DM participant {pid}: {dm_exc}")
                    except Exception as vps_exc:
                        logger.error(f"Giveaway VPS error for participant {pid}: {vps_exc}")

                ga["status"] = "ended"
                ga["successful_creations"] = count
                ga["vps_created"] = True

            persist_giveaways()

        except Exception as ga_loop_exc:
            logger.error(f"Error processing giveaway {gid}: {ga_loop_exc}")

# ════════════════════════════════════════════════════════════
#  BOT EVENTS
# ════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    logger.info(f"✅  Illumix Core online: {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Illumix Core VPS",
        ),
    )
    expire_loop.start()
    giveaway_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    c = message.content.lower()
    if any(k in c for k in ["pterodactyl install", "pterodactyl setup", "how to install pterodactyl"]):
        e = _embed("🦕  Pterodactyl Installation", color=C.INFO)
        e.add_field(name="📖  Official Docs",
                    value="https://pterodactyl.io/panel/1.0/getting_started.html", inline=False)
        await message.channel.send(embed=e)
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    try:
        guild  = member.guild
        before = invite_snapshot.get(str(guild.id), {})
        after  = await guild.invites()
        used   = None
        for inv in after:
            if inv.uses > before.get(inv.code, {}).get("uses", 0):
                used = inv; break
        if used and used.inviter:
            iid = used.inviter.id
            uid = str(iid)
            if uid not in users:
                users[uid] = {"points": 0, "inv_unclaimed": 0, "inv_total": 0, "unique_joins": []}
            if str(member.id) not in users[uid].get("unique_joins", []):
                users[uid].setdefault("unique_joins", []).append(str(member.id))
                users[uid]["inv_unclaimed"] += 1
                users[uid]["inv_total"]     += 1
                persist_users()
                try:
                    inv_u = await bot.fetch_user(iid)
                    e = _embed("🎉  New Unique Invite!",
                               f"**{member.name}** joined via your invite!", C.SUCCESS)
                    e.add_field(name="📨  Total", value=f"`{users[uid]['inv_total']}`", inline=True)
                    e.add_field(name="🎁  Unclaimed", value=f"`{users[uid]['inv_unclaimed']}`", inline=True)
                    await inv_u.send(embed=e)
                except Exception:
                    pass
        invite_snapshot[str(guild.id)] = {
            inv.code: {"uses": inv.uses, "inviter": inv.inviter.id if inv.inviter else None}
            for inv in after
        }
        save_json(INV_CACHE_FILE, invite_snapshot)
    except Exception as exc:
        logger.error(f"on_member_join: {exc}")

# ════════════════════════════════════════════════════════════
#  STARTUP
# ════════════════════════════════════════════════════════════
def load_config():
    global LOG_CHANNEL_ID
    cfg = load_json(os.path.join(DATA_DIR, "config.json"), {})
    LOG_CHANNEL_ID = cfg.get("log_channel_id")
    extra = load_json(os.path.join(DATA_DIR, "admins.json"), [])
    ADMIN_IDS.update(extra)
    ADMIN_IDS.update(MAIN_ADMIN_IDS)
    ADMIN_IDS.add(OWNER_ID)


if __name__ == "__main__":
    import asyncio  # needed only for async run

    async def main():
        load_config()
        persist_users()
        persist_vps()
        save_json(INV_CACHE_FILE, invite_snapshot)
        persist_giveaways()
        persist_renew()

        try:
            await bot.start(TOKEN)  # use await instead of bot.run
        except discord.LoginFailure:
            logger.error("❌  Invalid bot token — check your .env!")
        except Exception as exc:
            logger.error(f"❌  Bot failed to start: {exc}")
            # Optional: send error embed to a channel
            try:
                e = _embed(f"Bot failed to start: {exc}", color=0xFF0000)
                w = bot.get_channel(YOUR_CHANNEL_ID)  # replace with your channel ID
                if w:
                    await w.send(embed=e)
            except Exception as send_exc:
                logger.error(f"Failed to send error message: {send_exc}")

    async def handle_giveaway(parts, ga):
        count = 0
        for pid in parts:
            try:
                rec = await create_vps(
                    int(pid),
                    ga["vps_ram"],
                    ga["vps_cpu"],
                    ga["vps_disk"],
                    giveaway=True
                )
                if "error" not in rec:
                    count += 1
                    try:
                        p = await bot.fetch_user(int(pid))
                        e = _embed("🎉 You Got a VPS!", color=C.GIVEAWAY)
                        e.add_field(name="🆔 Container", value=f"`{rec['container_id']}`", inline=False)
                        e.add_field(name="👤 Username", value=f"```{rec['vps_user']}```", inline=True)
                        e.add_field(name="🔑 Password", value=f"```{rec['vps_pass']}```", inline=True)
                        e.add_field(name="🔗 SSH", value=f"```{rec['ssh']}```", inline=False)
                        await p.send(embed=e)
                    except Exception as send_exc:
                        logger.error(f"Failed to send VPS DM to user {pid}: {send_exc}")
            except Exception as vps_exc:
                logger.error(f"Giveaway VPS error for user {pid}: {vps_exc}")

        ga["status"] = "ended"
        ga["successful_creations"] = count
        ga["vps_created"] = True
        persist_giveaways()
# ════════════════════════════════════════════════════════════
#  BOT EVENTS
# ════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    logger.info(f"✅  Illumix Core online: {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        status=discord.Status.idle,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Illumix Core VPS",
        ),
    )
    expire_loop.start()
    giveaway_loop.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    c = message.content.lower()
    if any(k in c for k in ["pterodactyl install", "pterodactyl setup", "how to install pterodactyl"]):
        e = _embed("🦕  Pterodactyl Installation", color=C.INFO)
        e.add_field(name="📖  Official Docs",
                    value="https://pterodactyl.io/panel/1.0/getting_started.html", inline=False)
        await message.channel.send(embed=e)
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    try:
        guild  = member.guild
        before = invite_snapshot.get(str(guild.id), {})
        after  = await guild.invites()
        used   = None
        for inv in after:
            if inv.uses > before.get(inv.code, {}).get("uses", 0):
                used = inv; break
        if used and used.inviter:
            iid = used.inviter.id
            uid = str(iid)
            if uid not in users:
                users[uid] = {"points": 0, "inv_unclaimed": 0, "inv_total": 0, "unique_joins": []}
            if str(member.id) not in users[uid].get("unique_joins", []):
                users[uid].setdefault("unique_joins", []).append(str(member.id))
                users[uid]["inv_unclaimed"] += 1
                users[uid]["inv_total"]     += 1
                persist_users()
                try:
                    inv_u = await bot.fetch_user(iid)
                    e = _embed("🎉  New Unique Invite!",
                               f"**{member.name}** joined via your invite!", C.SUCCESS)
                    e.add_field(name="📨  Total", value=f"`{users[uid]['inv_total']}`", inline=True)
                    e.add_field(name="🎁  Unclaimed", value=f"`{users[uid]['inv_unclaimed']}`", inline=True)
                    await inv_u.send(embed=e)
                except Exception:
                    pass
        invite_snapshot[str(guild.id)] = {
            inv.code: {"uses": inv.uses, "inviter": inv.inviter.id if inv.inviter else None}
            for inv in after
        }
        save_json(INV_CACHE_FILE, invite_snapshot)
    except Exception as exc:
        logger.error(f"on_member_join: {exc}")

# ════════════════════════════════════════════════════════════
#  STARTUP
# ════════════════════════════════════════════════════════════
def load_config():
    global LOG_CHANNEL_ID
    cfg = load_json(os.path.join(DATA_DIR, "config.json"), {})
    LOG_CHANNEL_ID = cfg.get("log_channel_id")
    extra = load_json(os.path.join(DATA_DIR, "admins.json"), [])
    ADMIN_IDS.update(extra)
    ADMIN_IDS.update(MAIN_ADMIN_IDS)
    ADMIN_IDS.add(OWNER_ID)


if __name__ == "__main__":
    load_config()
    persist_users()
    persist_vps()
    save_json(INV_CACHE_FILE, invite_snapshot)
    persist_giveaways()
    persist_renew()
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        logger.error("❌  Invalid bot token — check your .env!")
    except Exception as exc:
        logger.error(f"❌  Bot failed to start: {exc}")
