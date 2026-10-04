---
name: qianzhi-subtitle
description: Generate accurate Chinese subtitles from a recording plus its verbatim script — transcribe with word-level timestamps, then align the script onto them to fix terminology and segmentation without overwriting what was actually said. Use when producing subtitles for a talk, lecture, or course video, especially when a script or teleprompter text exists.
---

# Subtitle alignment

Produce subtitles for a recording whose script you also have. The script is a
**glossary and a punctuation guide**, never a replacement for the audio.

## Pipeline

Before transcribing or aligning, **ask for the delivery format if the user has
not specified it**. Offer these three choices in one question:

1. 横屏：默认每行最多 **24** 个汉字等效宽度；
2. 竖屏：默认每行最多 **16** 个汉字等效宽度；
3. 其他要求：请填写每行最多多少个汉字等效宽度。

If the format is already known, use it without asking again. A previous video's
choice does not silently become the default for a new, unspecified video.
Each SRT cue is one physical text line; do not wrap a cue onto a second line.
Han/fullwidth characters count 1, ASCII halfwidth characters count 0.5.
This is a layout limit, not a license to split a word, title, or thought in
half. If an indivisible term exceeds the limit, flag it for manual review.

```bash
# 0. From video, extract audio first. A large .mp4 makes the free ASR services
#    fail, and the audio is all they use anyway.
ffmpeg -i talk.mp4 -vn -acodec libmp3lame -ar 16000 -ac 1 -b:a 64k talk.mp3

# 1. Transcribe WITH word-level timestamps (required — without them the
#    aligner has no pause data and no anchor points)
videocaptioner transcribe talk.mp3 --asr bijian --word-timestamps -o raw.srt

# 2. Save the verbatim script as plain text. Keep the Chinese punctuation
#    (。！？，、；：) — it drives segmentation. Paragraphs separated by newlines.

# 3. Align, and ask for the review sheet (4th argument — do not skip it).
#    Any python3 works; dependencies self-install on first run.
python3 <skill-dir>/scripts/align.py raw.srt script.txt out.srt review.md --max-chars 16

# 4. Read review.md and judge what the rules could not. See below.
```

Use `--max-chars 24` for horizontal video, `16` for vertical video, or the
user's custom maximum. The CLI's 24-char fallback is only for direct script
calls; the agent must still ask when the video's format is unspecified.
Put the final SRT alongside the source media with the same base name. Keep raw
ASR, the reference script, and `review.md` in a review subfolder if useful.

`videocaptioner` (`pip install videocaptioner`, needs Python 3.10–3.12 and
ffmpeg) does step 1. Steps 2–4 need no API key — the aligner is deterministic
Python and runs offline in about a second for a 35-minute talk.

**If `bijian` fails** (it returns `412 Precondition Failed` when the service is
unhappy — retrying and shrinking the file do not help), switch to
`--asr jianying`. Both are free. They differ in granularity — bijian emits one
CJK character per entry, jianying whole words — which the aligner normalizes,
so either is fine. Check the token count in its output: a 30-minute talk should
yield several thousand, and `Term/particle corrections applied` should be in
the dozens. Single digits mean the alignment failed rather than the recording
being clean.

No script available? Fall back to `videocaptioner subtitle raw.srt -o out.srt`,
which uses an LLM to re-segment and fix errors, and needs an API key. It is
markedly worse on terminology and proper nouns than script alignment.

## The governing principle

**The ASR text is the truth about *what was said*. The script only says *how to
spell it* and *where the clauses are*.** Recordings deviate from any script —
speakers ad-lib, add asides, rephrase. Therefore:

- Every word the ASR recognized is kept verbatim, with its own timestamp —
  including filler words and ad-libbed asides. **Never drop recognized
  content.** An early version treated the script as ground truth and replaced
  the text wholesale, which silently deleted every ad-lib and left stretches of
  audio with no subtitle at all.
- Script text the ASR never recognized is **never inserted** — we cannot
  confirm it was actually spoken at that point.
- The script overrides ASR text only where it is authoritative: proper nouns,
  abbreviations and numbers; the 的/地/得 particle; and verified homophones
  (identical pinyin, e.g. a name misheard as 钱志 for 钱智).
- **Override fixes spelling, never specificity.** A script token may replace an
  ASR token only when it is at least as specific. Where the ASR recognized a
  letter-bearing abbreviation and the script writes an ordinary word, the
  script is the vaguer of the two and must lose -- see Known limits.

## Segmentation rules

*Punctuation defines the units, length only subdivides them.* Parse into
clauses at the script's punctuation and at the speaker's pauses, then **pack
whole clauses onto a line** (`LINE_TARGET` ≈ 14 chars). Never split a clause to
hit a length target. Only a clause too long on its own gets cut, into balanced
pieces (25 chars → 14+11, not 20+5) at `jieba` word boundaries, recursing on
both halves.

For a narrow frame, first preserve the script/pause-based segmentation, then
enforce the requested hard cap on each cue. Prefer a clear clause turn over a
mathematical midpoint (`…问题｜如果…`, `…课程｜做了一张…`), and never strand a
standalone leading 给/用 at the end of a line. The final width pass must not
change the recognized words; timing of a new cut is estimated and needs audio
spot-checking. A hand-edited reference may itself exceed a new hard limit:
compare its break style, but do not copy its overlong lines.

- **Silence outranks every text heuristic.** A speaker reading a script pauses
  where the *meaning* breaks, so a pause is direct evidence of a semantic
  boundary. Word-level gaps are ~0ms inside a phrase (9480 of 10862 in a 35-min
  talk), so any gap ≥ `PAUSE_MS` (200) is deliberate — treat it as a
  first-class clause boundary, on par with the script's punctuation. It settles
  cases no wording analysis will: "反复用到"+"这两个原则" has **0ms** between
  them (one breath — carry the break past it), while "…什么样的人"+"如果硬着头
  皮去找呢" has **400ms** at exactly the script's period (leave the break — 如果
  opens the next clause). Render a pause boundary *without* a visible space.
- **Ad-libbed continuations extend the clause they complete** — a short ad-lib
  right after a script period ("反复用到" + "这两个原则") carries the break past
  it, or the continuation is orphaned from its verb. Do not carry it across a
  pause, nor when the run opens on a hesitation sound (呃/嗯/唉/哎), a leading
  word (如果/但是/所以), or an enumerator (第七个/第二件). Test the run's
  **head**, not its tail — checking the tail is what let "如果" get dragged onto
  the previous line, since 果 looks harmless.
- **Never merge a short line across a pause, and merge forwards as well as
  backwards.** A 3-char line is a stray fragment only if it runs on from a
  neighbour; "…工作职责" [440ms] "然后呢" is a deliberate short utterance. An
  enumerator pushed onto its own line belongs with what *follows*
  ("第七个 可能面临的约束和困难").
- **A script comma followed by ad-libbed words is not a gap.** "第一件，"
  written but "第一件事是…" spoken puts the comma inside a word as delivered,
  rendering as "第一件 事是". Suppress the space when the next token is
  ad-libbed. Don't try to detect this with `jieba` (it segments 第一件/事是 and
  sees nothing wrong) or with pauses (26 of 104 spaces in a hand-corrected
  reference sit at 0ms — spaces are semantic, not acoustic).
- **`jieba` only gets a vote where delivery diverged from the script.** Where
  both sides matched exactly, the script's punctuation is authoritative.
  Reading text with the commas stripped out, jieba glues clauses into plausible
  compounds ("业务线。大客户" → "线大", "SaaS公司，总部" → "公司总部") and must
  not win that argument.
- **Directionality**: 的/地/得 bind forward (never cut on either side);
  在/把/让/对/而/及 lead the next phrase (never end a line); 来/去/到/上/下/出/起
  are verb complements (never start a line); 了/着/过/呢/啊 trail (may end a
  line). A break absorbs a following bare trailing particle rather than
  stranding it ("更有意思" + "了"), gated on the particle ending a jieba word so
  "了解" is never cut into "了" | "解".
- **"Never end a line on X" and "never start a line on X" are different lists.**
  For a *script-derived* break only leading words (在/把/而/及…) may veto it;
  的/地/得 must not, because an ad-libbed 的 at a script sentence end is the
  sentence-final 的 — closing on "利润呢是比较稳定的" is right, and vetoing the
  break strands it as "的公司现在啊想干一件事".
- **Collapse terms the engine broke apart.** ASR emits "HRD" as `hr`+`t`, "B2B"
  as `b`+`to`+`b`, "RTCF" as `RT`+`cf` — several tokens against the script's
  one, so a 1:1 override never fires and the English-spacing rule even inserts a
  gap ("hr t"). When an aligned block is all-alphanumeric on *both* sides,
  replace the whole run with the script's spelling.
- **Don't discard ASR tokens that only look like punctuation.** The engine
  emits "30" and "%" separately; a blanket "drop tokens with no alphanumeric
  char" filter deleted all 22 of them, turning "从30%提到40%" into "从30提到40".
  Filter real punctuation (，、。) but keep %／℃／° and currency signs.

## Verifying a change

```bash
python3 <skill-dir>/scripts/eval_breaks.py reference.srt candidate.srt
```

Scores recall/precision/F1 over break positions against a hand-corrected
reference, and lists what each side breaks that the other doesn't. **Change one
rule at a time and re-score.** Every rule above was kept or discarded on that
number (94.6% → 95.9% F1, precision 97.0%, on a 35-minute talk). Two changes
that *sounded* obviously right measured worse and were narrowed or dropped:
blocking the break-carry before any clause-opening word (−0.4 F1 — as ad-libs,
pronouns and connectives more often continue the clause than start one, so only
enumerators are reliable), and POS-based 的/地/得 correction (see below).
Guessing at these rules without the score is how earlier regressions happened.

For the poster-video regression, compare against the author's hand-edited SRT:
the original aligner scored 89.6% break F1; the vertical-16 version scored
95.2%, with no cue above 16 equivalent characters. The older 24-character
horizontal path is byte-identical to the original output on the same inputs.
These are tests on one recording, not a universal accuracy guarantee. Do not
package the private recording, reference transcript, or edited SRT.

Without a reference, check: (1) last subtitle end ≈ audio duration via
`ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 audio.mp3`;
(2) spot-check corrections are real homophone/casing fixes, not deletions;
(3) read a random 30 seconds — ad-libs should still be present, not smoothed
away; (4) lint: no line over the chosen cap, no 1–3 char fragments, no adjacent pair
whose junction splits a jieba word. A char-level lint over-reports badly
(sentence-final 的, 技能's 能, 上一讲's 上 are all fine) — check word
boundaries, not characters.

## Then read the review sheet yourself

Pass a 4th argument and the aligner writes out everything the rules provably
cannot settle:

```bash
python3 <skill-dir>/scripts/align.py raw.srt script.txt out.srt review.md
```

**Do not stop at the generated file.** Three layers, each doing what it is
actually best at:

| Layer | Who | On what evidence |
|---|---|---|
| Segmentation | the script | pause timestamps -- objective, and better than judgment |
| Terminology, casing, numbers | the script | exact lookup in the verbatim text |
| 的/地/得, homophones, doubtful breaks | **you, reading** | meaning in context |

The third layer is not a fallback for the first two failing -- it is the part
that *needs* a reader and should never have been attempted with rules. Equally,
do not take work back from the first two: a pause timestamp beats your
intuition about where a clause ends, and the script beats your memory of how a
term is spelled.

`review.md` sections:

1. **的/地/得** -- every occurrence with context. Adverbial (modifier + VERB)
   wants 地; complement (VERB + degree word) wants 得; 地方/地区/天差地别 and
   得(dei3) are already right. Most need no change; the few that do are obvious
   on sight. (A hand-check found 15 in one 30-minute lecture, and correctly
   rejected 4 look-alikes where the following word was a noun: 明显的改变,
   更准确的说法, 空泛的摘要, 有的还在上升.)
2. **Words the subtitle uses that the script does not** -- split by phonetic
   distance, which is the single most useful sort order here:
   - **2a, near-homophone of what the script says at that point** -- same
     syllable count, every syllable sharing an initial or a final. At that
     distance the engine mis-heard and the script's spelling should win
     (提效 heard as 迪效, 增强 as 曾强, 最常 as 最长, 安迪 as 安德).
   - **2b, no close match** -- too far off to be a mis-hearing, so the speaker
     most likely said it. Judge from context; do NOT restore the script's
     wording just because it differs (情境 vs 情绪, 核实 vs 核定, 不错 vs
     不佳 were all confirmed as spoken).
   - **2c**, every ad-libbed stretch in full, as a backstop.
3. **Breaks with neither a pause nor punctuation behind them** -- chosen on
   length alone, the least certain of the segmentation.

Apply what you are confident about; raise genuinely ambiguous ones with the
user rather than silently picking. Say which category each change came from, so
they can judge the reasoning and not just the diff.

## Known limits

- **Never let a script pronoun overwrite a recognized abbreviation.** A speaker
  says "得给AI一些材料" where the script wrote "得给它一些材料"; the engine hears
  AI correctly, and the 1:1 override then *downgrades* it to 它. The bug was an
  `or` -- `is_termish(a_text) or is_termish(s_text)` -- which fires when EITHER
  side is a term, so a CJK script token wins over an ASCII ASR token. The guard
  is asymmetric and must stay keyed to **letters, not to alnum**: blocking every
  termish ASR token would also block "1" -> "一", which is a legitimate
  respelling. So: an ASR token containing an ASCII letter is never overwritten
  by a script token that contains none. The reverse still applies -- a script
  "AI" does correct an ASR "爱", and casing fixes ("facts" -> "FACTS") are
  untouched. One 3.5-minute recording had **three** such occurrences; on a
  short opener that is roughly one per minute, so do not treat it as rare.
- **Do not automate 的/地/得 with POS tagging.** It looks compelling and
  *fails*: Chinese noun-verb ambiguity (要求, 评估, 了解, 描述, 答复 all tag as
  `v`) turned "具体的要求" into "具体地要求" -- **0 right, 14 wrong** against a
  hand-corrected reference, strictly worse than doing nothing. A reader gets
  these right; a rule does not. Keep it in layer 3.
- **Do not auto-apply the near-homophone rule, only surface it.** The rule
  itself is sound -- 8/8 agreement with the author's own calls -- but applying
  it without a reader is not. Per character it scored **3 right, 8 wrong**:
  Chinese has few syllables, so common characters share a final by coincidence
  (的/到, 不/无, 常/当), and patching one character inside a word splices two
  words together ("非常" against the script's "相当" became "非当"). A
  word-level variant scored **0 right, 28 wrong**, because equal-length
  `difflib` replace blocks are not character correspondences and the mapping
  silently drifts. Keep it as a ranked suggestion in the review sheet.
- **A mis-recognition that is not a real word may not segment as one.** "曾强"
  is not in any dictionary, so `jieba` splits it into single characters and it
  never reaches section 2a. Read 2c as well; do not treat 2a as exhaustive.
- **The reference itself may not be self-consistent.** In one hand-corrected
  file "写的非常清楚" and "我简单的提一下" kept 的 where standard usage wants 得
  and 地. Surface such cases as a question; never silently "fix" the user's own
  edit.
- Roughly **4-5% of break positions** will still differ from a careful human
  edit, mostly by a few characters where there is neither a pause nor
  punctuation. That is the floor for layers 1-2; layer 3 narrows it, at the
  cost of reading.
