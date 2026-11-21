# spotify-now-bar

## Overview
A lightweight (~50 MB RAM, ~0% idle CPU) Samsung One UI–inspired "Now Bar" for Spotify on Windows, built with Python, PySide6, Windows SMTC, and the Spotify Web API (PKCE).

## Key Features
- **Zero-clutter top-center trigger**: Invisible when Spotify is closed, 3.5px dynamic album-art glow bar when playing, smooth spring drop-down on hover.
- **Adaptive One UI design**: Real-time dominant color extraction from album art, animated wavy progress slider, 7-bar harmonic waveform.
- **Full playback & library drawer**: Hover-expandable Spotify volume control, Heart/Like, Shuffle/Repeat, and slide-gesture drawer for Queue, Liked Songs, Playlists, and Search.

## Project Structure
- `now_bar.py`
- `spotify_api.py`
- `smtc_listener.py`
- `config.py`
- `requirements.txt`

> **Security Note:** Never commit `config.json` or `now_bar_error.log` to version control.

## Installation & Setup
```bash
pip install -r requirements.txt
python now_bar.py
```
