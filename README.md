# Smart NAS System

Smart NAS System is built around the Quectel Pi H1 smart control board, with CasaOS services and the OpenClaw agent at its core. Commands can be issued by voice or text, and the system automatically plans and invokes tools to handle file management, photo organization, media downloads, knowledge Q&A, and image processing — delivering a “one-sentence smart control” experience.

[English](README.md) | [中文](README_zh.md)

![UI preview](assets/image.png)

---

## What It Can Do

| Capability | Description | Example Command |
|------|------|----------|
| 📁 File Management | Move / organize / search files within the NAS | “Move xxx photos to the Family Album” |
| 🖼️ Photo Classification | Automatically classify and archive photos by content | “Help me classify the photos in the Family Album” |
| 🎬 Media Download | Say one sentence to download a video and auto-import it into the Jellyfin library | “Download the test video and put it in the Movies folder” |
| 🔍 Knowledge Base Q&A | Full-text document search + semantic photo search | “Where is the housing contract?” |
| 🎨 Image Filters | Apply vintage / Japanese / film styles in one click; output to a style subfolder of the original directory without overwriting the originals, with preview support | “Add a vintage filter to the Family Album” |
| 🗣️ Voice Assistant | The core entry point: web chat works out of the box; with a microphone, it also supports wake-word conversation and offline ASR/TTS | Use the wake word “小远同学” to start a conversation |

Model access uses an **OpenAI-compatible API** (DeepSeek, Qwen, OpenAI, etc.), and you only need one API Key.

---

## Runtime Environment

### Hardware

| Part | Quantity | Specification |
|------|------|------|
| Quectel Pi H1 smart control board | 1 unit | ARM64 octa-core, 8 GB RAM |
| USB-C power adapter | 1 unit | 27W PD, Type-C, 1.2 m cable (standard) |
| Micro HDMI cable | 1 cable | Micro HDMI 2.0, 1 m, HDMI-A (male) to HDMI-D (male) |
| Ethernet cable | 1 cable | Gigabit, 1 m |
| Display | 1 unit | 24-inch HDMI monitor |
| CPU cooling fan | 1 unit | Raspberry Pi 5 official active cooler with thermal pad |
| 2PIN PH1.25 speaker | 1 unit | 2030 cavity speaker, 8 Ω 2 W, square |
| USB microphone (optional) | 1 unit | 48 kHz high-sampling, 360° omnidirectional pickup |
| Onboard microphone | built-in | Quectel Pi H1 onboard microphone, PulseAudio source `regular0` |

> **Microphone note**: The speaker is used for TTS playback, while the microphone is used for wake word and voice commands. When no USB microphone is connected, the system automatically falls back to the **onboard microphone**, and the voice entry point remains usable. If neither is available, the web text mode still works.

### Software

| Project | Version |
|------|------|
| Operating system | Debian GNU/Linux 13 (trixie), kernel `6.6.52` |
| Container engine | Docker 29.7.2 / Docker Compose v5.5.0 |
| Application platform | CasaOS + OpenClaw (both run as containers) |
| Python | 3.10.15 |
| Speech inference | sherpa-onnx 1.13.7, numpy 1.24.3 |
| Audio/video and audio services | FFmpeg 7.1.5, PulseAudio 15.0 |

### Speech Models

Stored under `~/voice` by default.

| Model | Purpose |
|------|------|
| SenseVoice `model.int8.onnx` (INT8) | Default speech recognition, bilingual Chinese/English, used for both wake word and commands |
| Matcha `matcha-icefall-zh-en` | TTS acoustic model (bilingual Chinese/English) |
| Vocos `vocos-16khz-univ.onnx` | TTS vocoder |
| Conformer (optional, hotword) | Backup recognition engine with Chinese hotword support |

> Conformer is **not downloaded by the default install flow**. If it already exists in the local directory, it is a leftover from a manually enabled setup. It is only fetched when you explicitly pass `--download-conformer-model`.

---

## Project Setup

### 1. Clone the project code

Open a terminal on the smart control board and use git to clone the project code.

```bash
sudo apt update && sudo apt install -y git
git clone https://github.com/Quectel-Pi/demo-smart-nas-system.git NAS-Demo
```

After completion, a `NAS-Demo` folder should be created in the current directory.

### 2. Run the installation script

Run the following commands on the smart control board terminal. When the terminal shows the installation is complete, the deployment is complete.

```bash
cd ~/NAS-Demo
bash install.sh
```

Note: `install.sh` is idempotent and can be run repeatedly; it updates the model configuration while preserving existing services.

The interactive install is recommended (by default you only need to provide the API Key):

- Press Enter for Base URL to use the default `https://api.deepseek.com/v1`
- Paste the API Key (input is not echoed)
- Model ID defaults to `deepseek-chat`

Common provider parameters:

```text
DeepSeek: base-url=https://api.deepseek.com/v1                         model-id=deepseek-v4-flash
Qwen:     base-url=https://dashscope.aliyuncs.com/compatible-mode/v1   model-id=qwen-plus
OpenAI:   base-url=https://api.openai.com/v1                           model-id=gpt-4o-mini
```

If you later change the model settings in `~/NAS-Demo/.env` (for example `MODEL_BASE_URL`, `MODEL_API_KEY`, or `MODEL_ID`), run the following command to apply them immediately:

```bash
./oc.sh model-apply
```

If you changed voice-bridge related environment variables, restart the service after applying the new values:

```bash
sudo systemctl restart voice-bridge
```

### 3. Open the CasaOS console

Enter the following command in the terminal, copy the output link into a browser, and register a user:

```bash
bash ./oc.sh casaos-url
```

Then, on the CasaOS app page, confirm or configure other applications such as openclaw, Immich, Jellyfin, File Browser, and the web voice assistant.

### 4. Open the OpenClaw console and pair

Click to open the OpenClaw console in CasaOS.

First, you may see a self-signed certificate warning. Click “Continue” to proceed. Then run the following commands in the terminal to approve device pairing.

```bash
./oc.sh pair-list
./oc.sh pair-approve <request_id>
```

### 5. Sign in to Immich

Open Immich from CasaOS.

- On first run, click “Getting Started” and follow the page to create an admin account and password (using an email such as `admin@immich.app` is recommended)
- If you land directly on the login page, use the administrator account and password you created earlier
- Select the language; you can keep the default settings for the remaining steps
- In Immich: **Account Settings → API Keys → Create a new API Key (Select all) → Create**
- Write the key into the `IMMICH_API_KEY` field in `~/NAS-Demo/.env`

Run the following command in the terminal to register the Immich tool. On success, it will show “Completed: Immich Finished”.

```bash
cd ~/NAS-Demo
./oc.sh tools-immich-setup
```

### 6. First-time Jellyfin setup

Open Jellyfin from CasaOS.

Initial setup steps:

- Choose a language and continue
- Create an administrator account and password
- Add media libraries:
  - Type “Movies” → set the folder path to `/media/Movies`
  - Type “Shows” → set the folder path to `/media/TV Shows`
  - If the picker only shows `/media` and you cannot open subdirectories, return to the terminal and run `./oc.sh tools-media-setup` as mentioned above, then click “Refresh” above the folder list to browse again and select
- Keep the default metadata language and finish the wizard

After the wizard completes, create an API key:

- In Jellyfin: **Dashboard → Advanced → API Keys → New API Key**
- Write the generated key to the `JELLYFIN_API_KEY` field in `~/NAS-Demo/.env`
- Apply and verify the new key with one command:

```bash
cd ~/NAS-Demo
./oc.sh jellyfin-apply   # Restart voice-bridge and verify .env/runtime alignment + API auth
```

After that, videos downloaded by `media_downloader` are placed in `~/nas_share/downloads/Movies`, and Jellyfin will automatically scan and import them.

### 7. Enable the voice entry point

The voice/web chat is the default interaction entry point and must be enabled before first use.

Verify both entry points:

```bash
systemctl is-active voice-bridge          # Output active means it is working
curl -sS http://127.0.0.1:28082/healthz   # Return OK means it is working
journalctl -u voice-bridge -f             # Real-time logs (say “xiaoyuantongxue” into the microphone to wake it)
```

### 8. Try saying a sentence

Use the voice assistant by text or voice (or wake it with the wake word “xiaoyuantongxue”):

- Download the test video and play it
- Help me classify the photos in the Family Album
- Add a vintage filter to the photos under the Family Album
- Where is my housing contract?

Examples of  English commands:

```text
- Please classify the family album photos, preview first
- Please add a vintage filter to the family album photos, preview first
- Where is the housing contract?
- Download the test video
```

> Note: The English commands above have been verified in the current voice bridge. If a directory name itself is Chinese (for example `测试样例`), keeping the original directory name in the English reply is normal and does not mean speech recognition failed.

---

## Daily Maintenance

All maintenance commands are centralized through [oc.sh](oc.sh) (installed automatically during the one-click setup by [install.sh](install.sh)). Run `./oc.sh` or `./oc.sh help` at any time to view the full command list.

```bash
./oc.sh status                # Show the openclaw container status
./oc.sh logs                  # Show openclaw logs (last 120 lines by default)
./oc.sh logs all 60 --level=error   # Aggregate error logs from all services for the last 60 lines
./oc.sh health                # Gateway health check
./oc.sh url                   # Print the console access URL (including token)
./oc.sh casaos-url            # Print the CasaOS homepage URL
./oc.sh model                 # Show the current model configuration
./oc.sh model-apply           # Apply .env model changes immediately
./oc.sh deploy                # Redeploy the core services
./oc.sh reset                 # Reset the OpenClaw configuration to the factory template and restart
./oc.sh ui-fix                # Re-apply CasaOS Legacy card filtering
./oc.sh pair-list             # Show devices awaiting pairing
./oc.sh pair-approve <id>     # Approve device pairing
./oc.sh mic-mode status       # Show active microphone mode (.env + runtime)
./oc.sh mic-mode usb          # Switch to USB-first (fallback to onboard if unavailable)
./oc.sh mic-mode onboard      # Use onboard microphone only
./oc.sh docker-mirror         # Configure Docker registry mirrors (for image pull failures)
./oc.sh jellyfin-apply        # Apply Jellyfin .env config to voice-bridge and verify (restart + auth check)
```

> The voice bridge is a systemd service and is not part of `./oc.sh logs`; use `journalctl -u voice-bridge -f` to view real-time logs.

### Individual Features (Switch / Install)

```bash
./oc.sh tools-nas-setup       # File operation tool (nas_files)
./oc.sh tools-media-setup     # Media download tool (download_media)
./oc.sh tools-kb-setup        # Knowledge base tool (kb_search; also syncs the built-in test contract to 文档/)
./oc.sh tools-immich-setup    # Smart album MCP (immich; auto-imports built-in test photos after setup)
./oc.sh tools-sync            # Sync source code to the container runtime copy (required after source changes)
./oc.sh tools-photos-setup    # Sync sample photos to 家庭相册/测试样例 and auto-import into Immich (idempotent)
./oc.sh openclaw-app-deploy   # Install the OpenClaw web entry (CasaOS app)
./oc.sh immich-apply          # Deploy / repair Immich (CasaOS app)
./oc.sh immich-sync-jobs      # Trigger Immich face recognition / semantic search jobs (use when photos cannot be found)
./oc.sh jellyfin-deploy       # Deploy Jellyfin home theater
./oc.sh jellyfin-key-check    # Verify the Jellyfin API key is in effect (.env + voice bridge runtime + API auth)
./oc.sh nas-files-deploy      # Deploy NAS Files
./oc.sh voice-assistant-deploy  # Deploy Voice Assistant
```
---

## Project Structure

```text
NAS-Demo/
├── install.sh                  # One-click install / configuration entry point
├── oc.sh                       # Maintenance command wrapper
├── setup_docker_mirror.sh      # Configure Docker registry mirrors
├── deploy.sh                   # OpenClaw deployment script
├── docker-compose.yml          # Core service stack orchestration
├── .env(.example)              # Configuration (model API, gateway token)
├── openclaw.bootstrap.json     # OpenClaw factory configuration template
├── knowledge_base/             # Knowledge base service
├── media_downloader/           # Media download service
├── local_voice_chat/           # Voice assistant
├── voice_remote/               # Web chat assistant
├── redirect/                   # CasaOS icon redirect layer
├── casaos/                     # CasaOS frontend customization
├── assets/                     # Built-in samples (UI image / sample docs / sample photos)
├── logs/                       # Runtime logs
├── ops/
│   ├── scripts/                # Operations scripts
│   └── systemd/                # systemd service/timer and journald limit config
├── filebrowser-compose.yml     # NAS file browser
├── jellyfin-compose.yml        # Jellyfin
├── immich-compose.yml          # Immich
├── openclaw-compose.yml        # OpenClaw web entry
└── voice-assistant-compose.yml # Web chat assistant
```

---

## FAQ

| Symptom | Resolution |
|------|------|
| OpenClaw / Immich / Jellyfin do not appear on the CasaOS page, or the app does not open when clicked | Run `./oc.sh openclaw-app-deploy`, `./oc.sh immich-apply`, and `./oc.sh jellyfin-deploy` to re-register the app entries |
| `bash ./oc.sh casaos-url` does not open | If it says “CasaOS Web service not detected”, run `curl -fsSL https://get.casaos.io \| sudo bash` to install CasaOS |
| Voice Assistant (28083) reports 502 / connection refused when sending a message | The voice bridge (28082) is not running. Re-run `bash install.sh` to install it automatically, or manually run `cd ~/NAS-Demo/local_voice_chat && ./install_voice_bridge_service.sh` |
| Speech recognition returns single characters / no response | The systemd environment is missing PulseAudio. Confirm `voice-bridge.service` includes `LD_PRELOAD` + `PULSE_SERVER`, then run `sudo systemctl restart voice-bridge` |
| Need a quick USB/onboard microphone switch | Use `./oc.sh mic-mode usb` or `./oc.sh mic-mode onboard`; check current status with `./oc.sh mic-mode status` |
| Voice bridge installation reports “no python3 with numpy/sherpa_onnx found” | Dependencies are missing: run `python3 -m pip install --user sherpa-onnx numpy` and retry |
| Voice Assistant playback is blocked (Chrome/Edge shows “Block Audio”) | This is caused by the browser autoplay policy, not a service fault. Click the lock/permissions icon to the left of the address bar → **Permissions** → change **Autoplay** to **Audio and Video** |
| Immich photos cannot be found | The background CLIP task has not finished: trigger Smart Search in the admin UI, or run `./oc.sh immich-sync-jobs` |
| Photo classification hangs | `immich-machine-learning` may have exited: run `sudo docker start immich-machine-learning` |
| Changes in `.env` model configuration do not take effect | Run `./oc.sh model-apply` (reads `.env`, applies immediately, and restarts in about 10 seconds) |
| Changes to `JELLYFIN_API_KEY` in `.env` do not take effect, or Jellyfin 401 appears in logs | Run `./oc.sh jellyfin-apply` (restarts voice-bridge and verifies `.env`/runtime alignment and API auth) |
