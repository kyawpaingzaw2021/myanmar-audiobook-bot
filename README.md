# Myanmar Audiobook Bot

Telegram bot that converts Myanmar text → MP3 audiobook, posts to a Channel.

## Features
- Edge-TTS Neural voices (Nilar/Thiha)
- Per-user settings (rate/pitch/volume)
- ffmpeg MP3 concat (clean output)
- Multi-user whitelist
- Auto-cleanup + resume on crash
- systemd deployment ready

## Quick Start

```bash
cp .env.example .env
nano .env  # Fill in your values
