# MOC
Creating a Own server from Old mobile

Great — now we convert your entire system into a **professional GitHub-ready documentation** 📘
You can directly copy this into your repo `README.md`.

---

# MOC (MY OWN SERVER)

**Termux Remote Downloader + File Server (Telegram Controlled)**

---

# 🚀 Overview

This project turns an Android phone (via Termux) into a **remote-controlled download server + file manager**, accessible from anywhere using Telegram and a browser.

It combines:

* Telegram Bot (control system)
* FileBrowser (file management UI)
* Cloudflare Tunnel (remote access)
* Hybrid Downloader (`aria2` + `wget`)

---

# 🧠 Architecture

```text
Telegram → Bot (HTTP API)
        ↓
Termux (system.py)
        ↓
Download Engine
   ├─ aria2 (fast / torrent)
   └─ wget (fallback / stable)
        ↓
Storage (/storage/emulated/0)
        ↓
FileBrowser UI
        ↓
Cloudflare Tunnel (public link)
```

---

# ✨ Features

## 🤖 Telegram Control

* Send direct download links
* Support for:

  * HTTP/HTTPS links
  * Magnet links 🧲
  * Torrent files
* Folder selection via number input
* Subfolder navigation
* Start download (`/download`)
* Cancel download (`/cancel`)

---

## 📂 File Management

* Full file browser (upload/download/delete)
* Remote access via browser
* Login protected (username/password)

---

## ⚡ Download Engine

* **aria2**

  * Multi-threaded
  * Torrent + magnet support
* **wget**

  * Fallback for stability
* Automatic switching (hybrid system)

---

## 📊 Progress UI

* Live progress bar:

```
██████░░░░ 60%
```

* Lightweight updates (optimized for low-end devices)

---

## 🌐 Remote Access

* Public URL via Cloudflare Tunnel
* No port forwarding required

---

## 🔐 Security

* Telegram restricted via `CHAT_ID`
* FileBrowser login system

---

# 📱 Requirements

* Android device (recommended: 3GB RAM+)
* Termux (latest version from F-Droid)

---

# ⚙️ Installation

## 1. Setup Termux

```bash
termux-setup-storage
pkg update -y && pkg upgrade -y
pkg install -y python filebrowser cloudflared wget aria2
```

---

## 2. Clone or Add Script

Place `system.py` in:

```text
/storage/emulated/0/Download/system.py
```

---

## 3. Configure

Edit the file:

```python
BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

USERNAME = "your_username"
PASSWORD = "your_password"
```

---

## 4. Run

```bash
python /storage/emulated/0/Download/system.py
```

---

# 🚀 Usage

## Step 1: Start system

* Run script → bot becomes active

---

## Step 2: Send link in Telegram

Example:

```
https://example.com/file.mp4
```

---

## Step 3: Select folder

```
1. Download
2. Movies
3. Music
```

---

## Step 4: Start download

```
/download
```

---

## Step 5: Monitor progress

```
████░░░░░░ 40%
```

---

## Step 6: Cancel (optional)

```
/cancel
```

---

# 📁 Folder Navigation

* Enter number → open folder
* Navigate subfolders
* `/download` → save in current folder

---

# 🔄 Download Logic

```text
if magnet/torrent → aria2
if large file → aria2
if aria2 fails → fallback to wget
```

---

# ⚠️ Notes

* Torrent speed depends on seeders
* Cloudflare link changes every restart
* Keep Termux running for downloads
* Disable battery optimization for Termux

---

# 🔋 Background Execution

To keep system running:

```bash
termux-wake-lock
```

---

# 🧪 Troubleshooting

## aria2 not found

```bash
pkg install aria2
```

---

## Cloudflare not working

```bash
pkg install cloudflared
```

---

## Storage permission issue

```bash
termux-setup-storage
```

---

# 📌 Limitations

* Single active download (no queue)
* Basic progress display
* No resume after reboot
* Max 10 folders shown per level

---

# 🔮 Future Improvements

* Multiple download queue
* Resume downloads after restart
* Google Drive integration
* Advanced UI (buttons instead of numbers)
* Speed + ETA display
* File search

---

# 👨‍💻 Author

Karthikeyan

---

# 📜 License

MIT License (recommended)

---

# 💬 Final Note

This project is a lightweight, dependency-free solution designed for **low-end Android devices**, providing powerful remote file management and downloading capabilities without complex setup.
