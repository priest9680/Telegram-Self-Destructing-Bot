# 🤖 Telegram-Self-Destructing-Bot

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://www.python.org/downloads/)
[![Telegram](https://img.shields.io/badge/Telegram-Bot-blue.svg)](https://telegram.me/BotFather)
[![Encryption](https://img.shields.io/badge/Encryption-AES--256-green.svg)](https://en.wikipedia.org/wiki/Advanced_Encryption_Standard)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/priest9680/Telegram-Self-Destructing-Bot?tab=MIT-1-ov-file)

**Automatically save self-destructing media from Telegram with offline recovery and a dual-channel backup system**

[✨ Features](#-features) • [🛠 Installation](#-installation) • [📖 Usage](#-usage-guide) • [📋 Commands](#-command-reference) • [❓ FAQ](#-troubleshooting)

</div>

---
## Note 🏷️

**• You can contact the developer**

[![Contact Developer](https://img.shields.io/badge/Portfolio-Visit-blue?logo=github)](https://priest9680.github.io) 

---
### Bot Interface

```
🤖 Welcome to Self-Destructing Media Downloader Bot!

Features:
• Download photos, videos, documents
• Send files to your personal channel
• Progress tracking
• Offline media recovery
• Queue system for missed media
```

### Media Organization

```
Media/
├── 00 - A - @username - 123456789/
│   ├── 1700000000_abc123.jpg
│   └── 1700000001_def456.mp4
└── 01 - B - @user2 - 987654321/
    └── 1700000002_ghi789.mp4
```

---

## ✨ Features

### 🚀 Core Features
- Auto-detect and save **self-destructing (TTL) media**
- Dual-channel delivery (user channel + admin channel)
- Offline recovery for last **48 hours**
- Background queue with **retry logic (3 attempts)**
- Smart sender-based file organization

### 🔒 Security Features
- AES-256 encryption for session strings
- Encrypted SQLite database
- Environment-variable based secrets
- Whitelist-based access control
- No sensitive data in logs

### 📊 Management Features
- Queue statistics and processing controls
- Detailed logging system
- ZIP backup support
- Modular and extensible architecture

---

## 🛠 Installation

### Prerequisites
- Python **3.7+**
- Telegram account
- Bot Token from **@BotFather**
- API ID & API Hash from **my.telegram.org**

### Step-by-Step

#### 1. Clone & Setup
```bash
git clone https://github.com/priest9680/Telegram-Self-Destructing-Bot
cd Telegram-Self-Destructing-Bot

python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

#### 2. Environment Configuration

Create `.env` file:

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=1234567890:ABCDEFGHIJKLMNOPQRSTUVWXYZ
ADMIN_ID=5251410210

CHANNEL_ID=-1001234567890
SESSION_NAME=self_destruct
ENCRYPTION_KEY=your-strong-encryption-key

ALLOWED_USERS=5251410210,987654321
```

#### 3. Directory Setup
```bash
mkdir -p Media user_sessions logs backups
chmod 700 user_sessions
chmod 600 .env
```

#### 4. Run
```bash
python bot.py
```

---

## 📖 Usage Guide

### 👤 For Users
1. Request access from admin
2. `/login` and authenticate
3. Create a channel and run `/setmychannel -100xxxxxxxxxx`
4. Receive self-destructing media automatically
5. Recover missed media using `/checkmissed`

### 👑 For Admin
- Set global channel: `/setgchannel`
- Allow users: `/allow_user <id>`
- Monitor queues and logs
- Process missed media globally

---

## 📋 Command Reference

### User Commands

| Command | Description |
|------|------------|
| /start | Start the bot |
| /login | Login with Telegram account |
| /logout | Logout session |
| /setmychannel | Set personal channel |
| /mychannel | View channel |
| /mychanneltest | Test channel |
| /checkmissed | Recover last 48h |
| /queue_stats | Queue status |
| /process_queue | Process queue |
| /cancel | Cancel operation |

### Admin Commands

| Command | Description |
|------|------------|
| /setgchannel | Set global channel |
| /users | List users |
| /allow_user | Allow user |
| /disallow_user | Remove user |
| /queue_stats_all | Global stats |
| /process_queue_all | Process all |
| /status | System status |

---

## 🏗 Architecture

```
User Account ──▶ Bot Middleware ──▶ User Channel
      │                 │
      ▼                 ▼
  Queue (DB)        Admin Channel
      │
      ▼
 Local Storage
```

---

## 🔒 Security

- Encrypted sessions & DB
- Secure environment variables
- Input validation
- FloodWait safe handling
- No plaintext credentials

---

## 🚨 Troubleshooting

- **Invalid API_ID** → Verify from my.telegram.org
- **Wrong Channel ID** → Must start with `-100`
- **Media not saved** → Check `/mystatus`, `/queue_stats`
- **Bot unresponsive** → Check `/logs`, `/ping`

---

## 🔄 Deployment

### Docker
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t media-bot .
docker run -d --env-file .env media-bot
```

### Systemd
Supported for Linux servers (see docs section).

---

## 📄 License

MIT License

---

## ⚠️ Disclaimer

This project is for **personal and educational use only**.  
You are responsible for complying with Telegram TOS and local laws.

---

<div align="center">

⭐ **If you find this project useful, give it a star!** ⭐

</div>
