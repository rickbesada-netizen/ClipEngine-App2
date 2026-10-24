CLIP ENGINE
Free, runs on your own PC. Turns long videos and streams into vertical clips for clipping campaigns.

START
1. Unzip this folder somewhere permanent, e.g. Documents\clip-engine.
2. Double-click START.bat.
   - First run installs everything (Python if missing, then the app). If it installs Python,
     close the window and double-click START.bat again.
   - Your browser opens at http://127.0.0.1:5050. Keep the black window open while you work. (ClipEngine.exe has no window: it sits in the system tray instead.)

USE
1. Pick the campaign at the top (TJR is set up).
2. Find clips tab: press Browse next to a source to list its recent videos, tick some, Add selected.
   Or paste links (YouTube, Kick VODs and clips, Instagram reels) or paths to video files on your PC.
3. Set clips per video and length, press Find clips. Each video downloads, gets transcribed,
   and the best moments are cut to 1080x1920 with word-by-word captions and a hook title.
4. Clips tab: watch each clip, edit the caption (required hashtags are already in), Copy caption,
   Show file, then upload it to TikTok / Reels / Shorts yourself.
5. Press "I posted this", paste the post link. A countdown shows how long you have to submit
   it in Whop Content Rewards (30 minutes for TJR). Mark it submitted when done.

HOW IT PICKS MOMENTS
- YouTube's "most replayed" graph (what viewers rewatch)
- Loud / high-energy moments and laughter
- Strong opening lines, quotable sentences, campaign keywords
- Clips start and end on complete sentences
Streams over 30 minutes only get their most promising parts transcribed, so they're quick.

FRAMING
Auto picks one of: follow the face (talking head), facecam on top + screen below (trading
streams with a small facecam), or full frame on a blurred background. You can change it per clip
under "Re-cut or change the hook", along with exact start and end times.

SETTINGS TAB
- Transcription: tiny is fastest, small is more accurate. base is a good default.
- If YouTube says "confirm you're not a bot", or for Instagram: set "Use cookies from" to Firefox
  (signed in to that site, Firefox closed).
- Optional free local AI: install Ollama (ollama.com), run "ollama pull llama3.2", then pick it in
  Settings. It rates each moment and writes the hook title. Not required.
- "Delete downloaded source videos" frees disk space; clips are kept.

FILES
data\clips      finished clips (.mp4) and thumbnails
data\downloads  downloaded source videos
app\campaigns.json  campaign presets: rules, hashtags, sources, keywords. Add new campaigns here.

CAMPAIGN RULES TO REMEMBER (TJR)
- Clips must feature TJR and never show him negatively. No logos.
- Post only on Reels, TikTok or Shorts, from a trading-related account with a mostly
  US/CA/AU/UK/NZ audience. Caption must include #TJR.
- No bots, reposting or collab posts. No AI-generated content: the app only uses AI to
  transcribe, the footage is TJR's own, so don't add AI voices or AI visuals.
- Submit each link within 30 minutes of posting.

The caption font is Poppins (SIL Open Font License).
