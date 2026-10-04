# qianzhi-subtitle

Generate accurate Chinese subtitles from a recording **plus its verbatim
script**. The script supplies terminology and clause boundaries; the audio
supplies the truth about what was actually said.

On the latest poster-video test, the vertical-16 version improved break F1
from **89.6% to 95.2%** against the author's hand edit, while meeting the hard
line-width limit. This is one recording, not a universal guarantee.

## Why not just use an LLM to clean up the transcript?

Script alignment beats it on the things that matter here, and costs nothing:

| | LLM cleanup | Script alignment |
|---|---|---|
| Terminology, names, numbers | guesses; refuses some edits | exact — taken from the script |
| Filler words / stutters | inconsistent | kept or dropped by rule |
| Cost & latency | API calls, minutes | offline, ~1s for 35 min |
| Requires | API key | a script you already wrote |

These are complementary, not alternatives. Rules settle what timestamps and the
script can prove; a reader settles 的/地/得, homophone slips in ad-libs, and
doubtful breaks. The aligner emits a review sheet listing exactly those, so the
reading pass is targeted rather than a re-read of the whole transcript.

## Install

GitHub source: https://github.com/Jackyqian-ai/qianzhi-subtitle

On a new computer, clone directly into Codex's skill directory:

```bash
git clone https://github.com/Jackyqian-ai/qianzhi-subtitle.git ~/.codex/skills/qianzhi-subtitle
```

Alternatively, unzip the release and place its `qianzhi-subtitle` folder at
`~/.codex/skills/qianzhi-subtitle/` (or `$CODEX_HOME/skills/qianzhi-subtitle/`).
For Claude Code use `~/.claude/skills/qianzhi-subtitle/`. Back up an existing
folder before replacing it. Do not copy `.venv` from another machine; the
first run installs dependencies into a new private environment. The scripts
also run directly from an unpacked folder without installation.

## Use

```bash
# 1. Transcribe with word-level timestamps (needs `pip install videocaptioner`,
#    Python 3.10-3.12, and ffmpeg). --word-timestamps is REQUIRED.
videocaptioner transcribe audio.mp3 --asr bijian --word-timestamps -o raw.srt

# 2. Put the verbatim script in a .txt file, Chinese punctuation intact.

# 3. Align. The 4th argument is optional: a review sheet listing everything
#    the rules cannot settle (的/地/得, ad-libbed spans, uncertain breaks).
python3 scripts/align.py raw.srt script.txt out.srt review.md --max-chars 16
```

For a new video, if its format is not stated, ask: horizontal (24 characters
per line), vertical (16), or custom maximum. Put the final SRT next to the
media file, with the same base name. One cue occupies one text line.

Then **read `review.md`**. Rules handle segmentation and terminology; the
residual few percent needs a reader. See *Then read the review sheet yourself*
in `SKILL.md`.

First run creates `.venv/` beside the package and installs `jieba` and
`pypinyin` into it (~50 MB, once). Any Python 3.8+ can launch it; nothing
touches the system Python.

To score a change against a reference edit:

```bash
python3 scripts/eval_breaks.py reference.srt candidate.srt
```

## What's here

| File | |
|---|---|
| `SKILL.md` | The rules, with the reasoning and the measurements behind each |
| `AGENTS.md` | Same content, for agents that read `AGENTS.md` (Codex) |
| `scripts/align.py` | Self-bootstrapping entry point |
| `scripts/align_subs.py` | The aligner |
| `scripts/eval_breaks.py` | Break-position scorer |

## Portability

The aligner is plain Python with two pure-Python dependencies — no ffmpeg, no
network, no API key, no model. Only step 1 (transcription) needs
`videocaptioner` and ffmpeg. So the aligner runs anywhere Python does, and the
rules in `SKILL.md` are readable by any agent, or by you.
