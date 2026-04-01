# 💻 Illumix VPS Manager Bot 🚀

A **Discord bot to manage VPS containers via Docker.**
Admins can create, suspend, unsuspend, delete VPS, run giveaways, and auto-expire old VPS.


---

📦 Features

Admin Commands:

/create_vps      # Deploy a VPS with custom RAM, CPU, disk  
/suspend         # Suspend a VPS; stops container & notifies owner  
/unsuspend_vps   # Unsuspend a VPS; starts container & DM owner  
/delete_vps      # Force-delete a VPS from Docker and DB

Giveaways 🎉 – Random or multi-winner VPS distribution

Auto-Expiration ⏰ – Suspends expired VPS automatically

Logs & Persistence – Stores VPS info in JSON, logs actions


---

**⚙️ Requirements**

Python 3.12+
Docker installed
```
apt update 
apt install python3-pip -y
apt install docker.io -y
pip install discord.py python-dotenv psutil
```


---

# 🛠 Installation

# 1️⃣ Clone repo:
```
git clone https://github.com/zerioak/dvm-bot.git  
cd dvm-bot
```

# 2️⃣ Create virtual env (optional):
```
python -m venv venv  
source venv/bin/activate      # Linux/Mac  
venv\Scripts\activate         # Windows
```

# 3️⃣ Install dependencies:
```
pip install -r requirements.txt
```

# 4️⃣ Create .env:
```
BOT=YOUR_DISCORD_BOT_TOKEN
```

# 5️⃣ Configure bot.py:

```
GUILD_ID = 123456789012345678
MAIN_ADMIN_IDS = {123456789012345678}
OWNER_ID = 123456789012345678
SERVER_IP = "127.0.0.1"
IMAGE_UBUNTU = "jrei/systemd-ubuntu:22.04"
IMAGE_DEBIAN = "jrei/systemd-debian:12"
DEFAULT_RAM_GB = 8
DEFAULT_CPU = 2
DEFAULT_DISK_GB = 20
POINTS_PER_DEPLOY = 40
VPS_LIFETIME_DAYS = 15
ALLOWED_CHANNELS = [123456789012345678]
ADMIN_BYPASS_CHANNELS = True
```

---

# ▶️ Run the bot
```
python3 bot.py
```

# Expected console output:

INFO: Illumix Core online: Illumix VPS Manager#7377


---

# 📝 Commands

Command	Description	Admin Only

/create_vps	Deploy new VPS with custom specs	✅
/suspend	Suspend VPS temporarily	✅
/unsuspend_vps	Unsuspend VPS & notify owner via DM	✅
/delete_vps	Permanently delete VPS	✅



---

# 💾 Persistence

**VPS data stored in JSON:**
```
{
"container_id": "vps123",
"owner": 123456789,
"ram": 8,
"cpu": 2,
"disk": 20,
"active": true,
"suspended": false,
"expires_at": "2026-05-01T12:00:00Z"
}
```
**Auto-saves VPS data on create/suspend/unsuspend/delete**


---

# 📊 Logging

Optional Discord log channel

Logs all VPS actions: creation, suspension, deletion, unsuspension

Console prints internal errors


---

# 🔧 Contributing

1. Fork the repo


2. Create a feature branch


3. Commit your changes


4. Open a pull request


5. Ensure code follows project structure

---

# 📌 Notes

**Docker must be running ⚠️**

**Bot requires administrator permissions**

**Make sure channels & admins are set in bot.py**

---

# Owner
**Made with ❤️ by Zerioak**


