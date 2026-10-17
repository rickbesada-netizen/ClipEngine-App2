# Clip Engine

Free app that runs on your Windows PC and turns streams and long videos into vertical, captioned clips for clipping campaigns.

- Paste channel, VOD or clip links (YouTube, Kick, Instagram) or local video files
- Transcribes locally (faster-whisper), finds the moments people rewatch (YouTube "most replayed", loudness, laughter, strong lines, campaign keywords)
- Cuts 1080x1920 clips that follow the speaker's face or stack facecam over screen, with word-by-word captions and a hook title
- Writes each caption with the campaign's required hashtags
- Add any campaign by uploading its brief (PDF, Word, Google Doc link, screenshot or pasted text): it reads the rules, sources, hashtags, pay and deadline
- Channel kit per campaign: name and handle ideas (checks which YouTube handles are free), profile pictures, a YouTube banner, and a description and bios sized for YouTube, TikTok and Instagram
- YouTube upload batches: files named with their finished titles for one bulk upload in Studio; the app then watches your channel and fills in each Short's link and submission countdown automatically
- Optional direct upload through the official YouTube API (needs YouTube's free audit before videos can be public)
- Tracks posted links and counts down the submission deadline

## Download (Windows)
**[Download ClipEngine.exe](https://github.com/besadapat-debug/clip-engine/releases/latest/download/ClipEngine.exe)**, put it in its own folder (for example Documents\Clip Engine) and double-click it. Your browser opens Clip Engine.

- Windows may say "Windows protected your PC" because the app isn't code-signed: click **More info**, then **Run anyway**.
- Clips are saved in a `ClipEngine data` folder next to the exe.
- Starting takes 10-20 seconds. Keep the black window open while you work.

The exe is built and tested automatically by GitHub (see the Actions tab) whenever the code changes, and rebuilt weekly so the video downloader stays current.

## Run from source instead
Download the code (green **Code** button > **Download ZIP**), unzip it and double-click `START.bat`.

See `README.txt` for the full guide. Campaign presets live in `app/campaigns.json` (TJR included).

Everything is free and stays on your computer.
