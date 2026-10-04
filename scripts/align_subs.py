"""
Align a verbatim script to word-level ASR timestamps to correct terminology,
particle usage (的/地/得), and segmentation -- WITHOUT overwriting the audio
content with the script.

Design principle (do not violate): the ASR-recognized text is the source of
truth for WHAT WAS SAID, because real recordings deviate from any prepared
script (ad-libs, added asides, skipped lines). The script is only a reference
used for narrow, high-confidence corrections:
  - proper nouns / abbreviations / numbers (ASCII alnum tokens): fix
    homophone errors and casing (e.g. "facts" -> "FACTS", "八" -> "8")
  - the 的/地/得 particle: ASR often collapses all three to "的"; restore the
    grammatically correct one from the script when aligned 1:1
  - segmentation: use the script's punctuation to decide where to break
    subtitle lines, but only at positions that are actually anchored to
    recognized ASR content

Content the ASR heard but the script does not contain (ad-libbed words) is
ALWAYS kept verbatim with its own ASR timestamp -- never dropped. Content the
script has but the ASR never recognized is ALWAYS dropped -- never inserted,
since we can't confirm it was actually spoken at that point. This asymmetry
is intentional: it prevents subtitle text from silently diverging from what
viewers actually hear, which is a strictly worse failure mode than an
occasional uncorrected ASR error.
"""
import re
import sys
import math
import difflib
import logging
import argparse
import unicodedata
from pypinyin import lazy_pinyin, Style

import jieba
jieba.setLogLevel(logging.WARN)

parser = argparse.ArgumentParser(description='Align word-timestamped ASR to a reference script.')
parser.add_argument('wordlevel_srt')
parser.add_argument('script_txt')
parser.add_argument('out_srt')
parser.add_argument('review_md', nargs='?')
parser.add_argument('--max-chars', type=int, default=24,
                    help='maximum Chinese-character-equivalent width per subtitle (default: 24)')
args = parser.parse_args()
if not 4 <= args.max_chars <= 80:
    parser.error('--max-chars must be between 4 and 80')
SRT_PATH = args.wordlevel_srt
SCRIPT_PATH = args.script_txt
OUT_PATH = args.out_srt
REVIEW_PATH = args.review_md
MAX_CHARS = args.max_chars

WORD_RE = re.compile(r'[A-Za-z0-9]+')
CJK_RE = re.compile(r'[\u4e00-\u9fff]')
STRONG_BREAK = set('。！？\n')
WEAK_BREAK = set('，、；：')
PARTICLES = {'的', '地', '得'}


def parse_srt(path):
    text = open(path, encoding='utf-8').read()
    blocks = re.split(r'\n\s*\n', text.strip())
    entries = []
    for b in blocks:
        lines = b.strip().splitlines()
        if len(lines) < 2:
            continue
        m = re.match(r'(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)', lines[1])
        if not m:
            continue

        def to_ms(t):
            h, mi, s = t.split(':')
            s, ms = s.split(',')
            return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(ms)

        start = to_ms(m.group(1))
        end = to_ms(m.group(2))
        content = ' '.join(lines[2:]).strip()
        entries.append((content, start, end))
    return entries


# Symbols that carry meaning rather than punctuating: the ASR emits these as
# their own tokens ("30" then "%"), and dropping them silently loses content
# the speaker actually said ("从30%提到40%" -> "从30提到40").
MEANINGFUL_SYMBOLS = set('%％℃℉°$￥€£#&+')


def is_punct_only(t):
    if not t:
        return True
    if any(c in MEANINGFUL_SYMBOLS for c in t):
        return False
    return all((not c.isalnum()) for c in t)


def is_termish(t):
    return bool(WORD_RE.fullmatch(t))


def is_abbrev(t):
    """An ASCII token containing at least one LETTER: AI, HR, JD, HRBP, B2B.

    Deliberately excludes bare numbers. The two are not interchangeable here:
    a script legitimately respells a digit the engine emitted ("1" -> "一"),
    but it must never respell a recognized abbreviation (see the override
    guard below).
    """
    return is_termish(t) and any(c.isalpha() for c in t)


def is_single_cjk(t):
    return len(t) == 1 and bool(CJK_RE.fullmatch(t))


def same_reading(a, b):
    # homophone check (tone-insensitive) for single-character CJK substitutions,
    # e.g. ASR mishearing a name: 志 vs 智 (both "zhi")
    return lazy_pinyin(a, style=Style.NORMAL) == lazy_pinyin(b, style=Style.NORMAL)


def near_reading(a, b):
    """True if two readings are close enough that the difference is far more
    likely a mis-recognition than a different word the speaker chose.

    Phonetic distance is the discriminator between the two ways delivery can
    depart from the script. A near-miss is the engine's error and the script
    should win (提效 heard as 迪效, 增强 as 曾强, 最常 as 最长, 安迪 as 安德).
    A distant reading is the speaker saying something else, and the script must
    NOT overwrite it (情境 vs 情绪, 核实 vs 核定, 不错 vs 不佳) -- those need a
    reader weighing the context, so they go to the review sheet instead.

    "Close" = same syllable count, and every syllable shares its initial or
    its final with the other. Validated against every such case in a
    hand-checked lecture: 8/8 agreement with the author's own calls.
    """
    if not a or not b:
        return False
    ia = lazy_pinyin(a, style=Style.INITIALS, strict=False)
    fa = lazy_pinyin(a, style=Style.FINALS, strict=False)
    ib = lazy_pinyin(b, style=Style.INITIALS, strict=False)
    fb = lazy_pinyin(b, style=Style.FINALS, strict=False)
    if not (len(ia) == len(ib) == len(fa) == len(fb)):
        return False
    for i1, f1, i2, f2 in zip(ia, fa, ib, fb):
        if i1 == i2 and f1 == f2:
            continue
        if f1 and f1 == f2:      # same final ("ti"/"di", "zeng"/"ceng")
            continue
        if i1 and i1 == i2:      # same initial ("di"/"de")
            continue
        return False
    return True


def norm(t):
    return t.lower() if WORD_RE.fullmatch(t) else t


# 1. Parse ASR word-level entries, keep only content tokens
raw_entries = parse_srt(SRT_PATH)
asr_tokens = []  # list of (text, start, end)
for text, start, end in raw_entries:
    t = text.strip()
    if not t or is_punct_only(t):
        continue
    # Engines disagree on granularity: bijian emits one CJK character per
    # entry, jianying emits whole words ("各位", "钱志"). The script side is
    # tokenized per character, and difflib can only align like with like -- a
    # word token never equals a character atom, so a word-level transcript
    # silently aligns almost nothing. Split multi-character CJK runs and share
    # the span between them; gaps inside a word become 0ms, which is correct.
    if len(t) > 1 and all(CJK_RE.match(c) for c in t):
        span = end - start
        for i, ch in enumerate(t):
            asr_tokens.append((ch,
                               start + span * i // len(t),
                               start + span * (i + 1) // len(t)))
    else:
        asr_tokens.append((t, start, end))

print(f"ASR content tokens: {len(asr_tokens)}", file=sys.stderr)

# 2. Tokenize script into ordered atoms: (text, kind)
script = open(SCRIPT_PATH, encoding='utf-8').read()
tokens = []
i, n = 0, len(script)
while i < n:
    c = script[i]
    m = WORD_RE.match(script, i)
    if m:
        tokens.append((m.group(), 'content'))
        i = m.end()
        continue
    if CJK_RE.match(c):
        tokens.append((c, 'content'))
        i += 1
        continue
    if c in STRONG_BREAK:
        tokens.append((c, 'break_strong'))
        i += 1
        continue
    if c in WEAK_BREAK:
        tokens.append((c, 'break_weak'))
        i += 1
        continue
    i += 1  # whitespace / other punctuation -> ignore

content_atoms = [t for t in tokens if t[1] == 'content']
print(f"Script content atoms: {len(content_atoms)}", file=sys.stderr)

# script atom -> break that immediately follows it ('strong' / 'weak' / None)
atom_break_after = [None] * len(content_atoms)
content_idx = -1
for tok_text, kind in tokens:
    if kind == 'content':
        content_idx += 1
    elif kind == 'break_strong':
        if content_idx >= 0:
            atom_break_after[content_idx] = 'strong'
    elif kind == 'break_weak':
        if content_idx >= 0 and atom_break_after[content_idx] != 'strong':
            atom_break_after[content_idx] = 'weak'

asr_norm = [norm(t[0]) for t in asr_tokens]
script_norm = [norm(t[0]) for t in content_atoms]

sm = difflib.SequenceMatcher(None, asr_norm, script_norm, autojunk=False)
opcodes = sm.get_opcodes()
print(f"Opcodes: {len(opcodes)}", file=sys.stderr)

# Per-ASR-token override text (None = keep ASR's own text) and break signal
asr_override = [None] * len(asr_tokens)
asr_break_after = [None] * len(asr_tokens)
asr_adlib = [False] * len(asr_tokens)  # recognized but absent from script
# tokens inside a block where delivery and script diverge in length: here the
# script's punctuation cannot be trusted to land on a real word boundary
asr_uncertain = [False] * len(asr_tokens)

n_override = 0
for tag, i1, i2, j1, j2 in opcodes:
    if tag == 'equal':
        for k in range(i2 - i1):
            a_text = asr_tokens[i1 + k][0]
            s_text = content_atoms[j1 + k][0]
            # normalized-equal but raw text differs (e.g. casing): fix termish tokens
            if a_text != s_text and is_termish(a_text) and is_termish(s_text):
                asr_override[i1 + k] = s_text
                n_override += 1
            asr_break_after[i1 + k] = atom_break_after[j1 + k]
    elif tag == 'replace':
        n_asr, n_script = i2 - i1, j2 - j1
        if n_asr == n_script:
            for k in range(n_asr):
                a_text = asr_tokens[i1 + k][0]
                s_text = content_atoms[j1 + k][0]
                if a_text != s_text:
                    if is_abbrev(a_text) and not is_abbrev(s_text):
                        # The engine recognized an abbreviation that the script
                        # writes as an ordinary word or pronoun -- "得给AI一些
                        # 材料" scripted as "得给它一些材料". Here the script is
                        # LESS specific than the delivery, so overriding would
                        # swap what was actually said for a vaguer word: the
                        # exact opposite of what script override exists to do.
                        # Override fixes SPELLING, never specificity, so the
                        # direction abbrev -> non-abbrev is always wrong.
                        # (The reverse still applies: a script "AI" does
                        # correct an ASR "爱", and "一" still corrects "1".)
                        pass
                    elif is_termish(a_text) or is_termish(s_text):
                        asr_override[i1 + k] = s_text
                        n_override += 1
                    elif a_text in PARTICLES and s_text in PARTICLES:
                        asr_override[i1 + k] = s_text
                        n_override += 1
                    elif is_single_cjk(a_text) and is_single_cjk(s_text) and same_reading(a_text, s_text):
                        asr_override[i1 + k] = s_text
                        n_override += 1
                asr_break_after[i1 + k] = atom_break_after[j1 + k]
        else:
            # Lengths differ: too ambiguous to map term-for-term safely.
            # Only carry the break signal at the block boundary; leave all
            # ASR text in this block untouched (matches ASR = what was said).
            for k in range(i1, i2):
                asr_uncertain[k] = True
            # ...with one exception. When BOTH sides are made purely of
            # alphanumeric tokens, this is a term the engine broke apart or
            # spelled out ("hr"+"t", "b"+"to"+"b", "RT"+"cf") against the
            # script's single "HRD" / "B2B" / "RTCF". Spelling terms is exactly
            # what the script is for, so collapse the run onto its wording.
            script_run = [content_atoms[k][0] for k in range(j1, j2)]
            # trim CJK the speaker added around the term ("的"+"hr"+"d")
            a, b = i1, i2
            while a < b and not is_termish(asr_tokens[a][0]):
                a += 1
            while b > a and not is_termish(asr_tokens[b - 1][0]):
                b -= 1
            if (b > a and n_script > 0
                    and all(is_termish(asr_tokens[k][0]) for k in range(a, b))
                    and all(is_termish(t) for t in script_run)):
                asr_override[a] = ''.join(script_run)
                for k in range(a + 1, b):
                    asr_override[k] = ''
                n_override += 1
            if n_asr > 0:
                asr_break_after[i2 - 1] = atom_break_after[j2 - 1]
    elif tag == 'delete':
        # ASR-only, ad-libbed content: text kept verbatim, flagged so that a
        # break landing just before it can be deferred past it (see below)
        for k in range(i1, i2):
            asr_adlib[k] = True
    # 'insert' (script-only, never recognized by ASR): nothing to attach to,
    #   intentionally dropped

MAX_CJK = 20
SEP = ' '
# Hesitation sounds. These start a fresh utterance, so a clause must never be
# extended across one, and a break must never be deferred past one.
HESITATION = set('呃嗯唉哎')
# Silence long enough to count as a spoken boundary. Intra-phrase gaps in the
# word-level ASR output are overwhelmingly 0ms, so anything at this scale is a
# deliberate pause rather than articulation noise.
PAUSE_MS = 200
# Aspect/mood particles: they close the phrase they belong to, cut AFTER them.
CUT_AFTER = set('了着过呢吧吗啊呀嘛')
# Leading function words: they introduce the phrase that follows, so cut
# BEFORE them -- leaving "在" or "把" stranded at end of line reads badly.
CUT_BEFORE = set('在把让被对从和与及但而就也还又并是能会要为跟同因所虽如若既每另')
# 的/地/得 bind forward to the word they modify: cutting on either side of them
# strands the modifier from its head. Never cut adjacent to these.
STICKY = set('的地得')
# Directional/resultative complements: they close a verb ("接下来", "提上去",
# "帮助到"), so they must never begin a line.
COMPLEMENTS = set('来去到上下出起过完住掉')
# A line must not end on a word that leads the next phrase, nor start on a
# particle or complement that trails the previous one.
NEVER_END = CUT_BEFORE | STICKY
NEVER_START = CUT_AFTER | STICKY | COMPLEMENTS | MEANINGFUL_SYMBOLS
# Words that open a clause. Derived from what actually follows the breaks in a
# hand-corrected reference: a subject pronoun/demonstrative, or a connective.
# When length forces a cut, putting it just before one of these is what a human
# editor does -- these are only a *preference*, never a reason to break.
LEADING_WORDS = {
    '这', '这个', '这些', '这样', '这里', '这种', '这份', '那', '那个', '那些', '那么',
    '我', '我们', '你', '你们', '他', '她', '它', '他们', '它们', '大家', '咱们',
    '但是', '但', '所以', '因为', '因此', '于是', '而且', '并且', '然后', '接下来',
    '另外', '同时', '还有', '不过', '其实', '当然', '首先', '其次', '最后', '总之',
    '比如', '比如说', '例如', '反过来', '也就是说', '换句话说', '如果', '既然', '虽然',
}
# ...and words that close one: never treat these as a reason to cut before them
# ("画像" | "而已" is wrong even though 而 usually leads).
TRAILING_WORDS = {'而已', '罢了', '等等', '之类', '什么的', '为止', '来说', '而言'}

def _lead_bonus(word):
    """How strongly a cut just before `word` is preferred."""
    if not word or word in TRAILING_WORDS:
        return 0
    if word in LEADING_WORDS:
        return 4
    if word[0] == '第':          # 第二件 / 第七个 / 第四步 -- always opens a clause
        return 4
    if word[0] in CUT_BEFORE:
        return 2
    return 0

# Defer a break that lands immediately before a short ad-libbed run.
# The script's punctuation says "sentence ends here", but the speaker kept
# going -- and a short continuation almost always completes the clause that
# just ended (verb + ad-libbed object: "反复用到" + "这两个原则"). Breaking at
# the script's position would orphan that continuation onto the next line.
# Long ad-libs are left alone: those are genuinely new sentences.
MAX_ADLIB_CARRY = 12
n_tok = len(asr_tokens)
deferred = list(asr_break_after)
n_deferred = 0
for i in range(n_tok):
    brk = asr_break_after[i]
    if brk is None or i + 1 >= n_tok or not asr_adlib[i + 1]:
        continue
    j = i + 1
    while j < n_tok and asr_adlib[j]:
        # Never carry the break *across* a pause. "…严重的损失" + "啊" [630ms]
        # + "那第二个挑战" -- 啊 finishes the sentence, 那 opens the next one,
        # and the silence between them says exactly where to stop.
        if j > i + 1 and asr_tokens[j][1] - asr_tokens[j - 1][2] >= PAUSE_MS:
            break
        j += 1
    run_len = sum(len(asr_tokens[k][0]) for k in range(i + 1, j))
    if run_len > MAX_ADLIB_CARRY:
        continue
    if any(asr_break_after[k] is not None for k in range(i + 1, j)):
        continue
    # Don't move the break onto a word that leads the *next* phrase ("增长" +
    # "而"). 的/地/得 are deliberately NOT checked here: at a script sentence
    # end an ad-libbed 的 is the sentence-final 的 ("利润呢是比较稳定的"), and
    # closing the line on it is strictly better than the alternative, which is
    # to strand it at the head of the next line ("的公司现在啊想干一件事").
    tail = asr_tokens[j - 1][0]
    if tail and tail[-1] in CUT_BEFORE:
        continue
    # The speaker's own silence is the most reliable boundary evidence there
    # is. If they paused at the script's break position and only then kept
    # talking, the ad-lib opens a new utterance rather than completing the
    # clause that just ended -- leave the break where the script put it.
    # ("...什么样的人" [400ms] "如果硬着头皮去找呢" -- 如果 belongs to what
    # follows, whereas "反复用到" + "这两个原则" is spoken with no gap at all.)
    if asr_tokens[i + 1][1] - asr_tokens[i][2] >= PAUSE_MS:
        continue
    # Same conclusion from the wording, for the many boundaries the speaker
    # runs straight through: a run opening on a hesitation sound, on a word
    # that leads a phrase ("如果", "但是"), or on a clause opener -- a subject
    # pronoun or an enumerator like "第七个" -- starts the next clause rather
    # than finishing this one, so the break stays where the script put it.
    head_tok = asr_tokens[i + 1][0]
    run_text = ''.join(asr_tokens[k][0] for k in range(i + 1, j))
    first_word = jieba.lcut(run_text)[0] if run_text else ''
    if first_word in TRAILING_WORDS:
        pass            # "而已" closes the sentence it follows -- carry on
    elif head_tok and head_tok[0] in HESITATION:
        continue
    elif first_word[:1] == '第':
        # An enumerator ("第七个", "第二件") always opens what follows; pulling
        # it back onto the previous line buries the start of a new item.
        # Pronouns and connectives are NOT treated this way -- as ad-libs they
        # more often continue the current clause than start a new one.
        continue
    elif head_tok and head_tok[0] in CUT_BEFORE:
        continue
    deferred[i] = None
    existing = deferred[j - 1]
    deferred[j - 1] = 'strong' if (brk == 'strong' or existing == 'strong') else (existing or brk)
    n_deferred += 1
asr_break_after = deferred
print(f"Breaks deferred past ad-libbed continuations: {n_deferred}", file=sys.stderr)

# The script's punctuation is placed against the script's wording, which is not
# always the wording that was spoken ("这个岗位名，" in the script vs "这个岗位
# 名称" as delivered). A break inherited from such a spot can land inside a real
# word, so validate every break against the actual spoken text and nudge it to
# the nearest following word boundary (or drop it if none is close).
_final_text = ''.join(
    (asr_override[k] if asr_override[k] is not None else asr_tokens[k][0])
    for k in range(n_tok)
)
_tok_end = []
_acc = 0
for k in range(n_tok):
    _acc += len(asr_override[k] if asr_override[k] is not None else asr_tokens[k][0])
    _tok_end.append(_acc)
_word_ends, _acc = set(), 0
_word_span = {}     # char index -> length of the word covering it
for w in jieba.lcut(_final_text):
    for _o in range(len(w)):
        _word_span[_acc + _o] = len(w)
    _acc += len(w)
    _word_ends.add(_acc)

n_nudged = n_dropped = 0
adjusted = list(asr_break_after)
for k in range(n_tok):
    brk = asr_break_after[k]
    if brk is None or _tok_end[k] in _word_ends:
        continue
    # Only second-guess the script's punctuation where delivery actually
    # diverged from it -- an ad-libbed word, or a block whose lengths didn't
    # match ("这个岗位名，" written vs "这个岗位名称" spoken). Where both sides
    # matched the script exactly, the script's own clause boundary is
    # authoritative: jieba, reading text with the commas stripped out, will
    # cheerfully glue two clauses into a plausible compound ("业务线。大客户"
    # -> "线大", "SaaS公司，总部" -> "公司总部") and must not win that argument.
    nxt = k + 1
    if not (asr_uncertain[k] or (nxt < n_tok and (asr_adlib[nxt] or asr_uncertain[nxt]))):
        continue
    if _word_span.get(_tok_end[k], 0) > 3:
        continue
    moved = False
    for k2 in range(k + 1, min(k + 5, n_tok)):
        if _tok_end[k2] in _word_ends:
            if adjusted[k2] is None:
                adjusted[k2] = brk
            adjusted[k] = None
            moved = True
            n_nudged += 1
            break
    if not moved:
        adjusted[k] = None
        n_dropped += 1
asr_break_after = adjusted
print(f"Breaks nudged to word boundary: {n_nudged}, dropped: {n_dropped}", file=sys.stderr)

print(f"Term/particle corrections applied: {n_override}", file=sys.stderr)
deletes = [asr_tokens[k][0] for tag, i1, i2, j1, j2 in opcodes if tag == 'delete' for k in range(i1, i2)]
print(f"Ad-libbed ASR tokens kept (not in script): {len(deletes)}", file=sys.stderr)
print("sample:", deletes[:30], file=sys.stderr)


def fmt_ts(ms):
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"




def _is_clause_space(s, i):
    """True if s[i] is a clause separator rather than English word spacing."""
    if i < 0 or i >= len(s) or s[i] != SEP:
        return False
    before = s[i - 1] if i > 0 else ''
    after = s[i + 1] if i + 1 < len(s) else ''
    # "Revenue Leader" is one phrase; "在广州 成立了3年" is two clauses
    return not (before.isascii() and before.isalnum()
                and after.isascii() and after.isalnum())


def _valid_cut(s, cut):
    n = len(s)
    if cut <= 1 or cut >= n:
        return False
    if s[cut - 1] in NEVER_END:
        return False
    if s[cut] in NEVER_START:
        return False
    # never split an English phrase at its internal space
    if s[cut] == SEP and not _is_clause_space(s, cut):
        return False
    if cut > 0 and s[cut - 1] == SEP and not _is_clause_space(s, cut - 1):
        return False
    return True


def _glued(left, right):
    """True if the join between two clauses falls inside a single word."""
    tail, head = left[-4:].replace(SEP, ''), right[:4].replace(SEP, '')
    if not tail or not head:
        return False
    probe = tail + head
    pos, cut = 0, len(tail)
    for w in jieba.lcut(probe):
        if pos == cut:
            return False
        pos += len(w)
    return pos != cut


def word_boundaries(s):
    """Char indices where a line may break without splitting a word."""
    bounds, idx = set(), 0
    for w in jieba.lcut(s):
        idx += len(w)
        bounds.add(idx)
    # a separator we inserted is always a legal boundary on both sides
    for i, ch in enumerate(s):
        if ch == SEP:
            bounds.add(i)
            bounds.add(i + 1)
    return sorted(b for b in bounds if 0 < b < len(s))


def word_at(s):
    """Map each char index to the word starting there."""
    out, idx = {}, 0
    for w in jieba.lcut(s):
        out[idx] = w
        idx += len(w)
    return out




def find_cut(remaining, target):
    """Pick the most natural split point near `target`, never mid-word."""
    candidates = []
    heads = word_at(remaining)
    for b in word_boundaries(remaining):
        if not _valid_cut(remaining, b):
            continue
        # A script-derived clause boundary is preferred, but only as a nudge --
        # a distant separator must never outrank a nearby word boundary, or the
        # resulting line blows past the length limit.
        head = heads.get(b)
        if head in TRAILING_WORDS:
            continue        # "…画像" | "而已" strands a phrase-closing word
        is_sep = _is_clause_space(remaining, b) or _is_clause_space(remaining, b - 1)
        bonus = 6 if is_sep else _lead_bonus(head)
        candidates.append((abs(b - target) - bonus, b))
    if candidates:
        return min(candidates)[1]

    # no legal word boundary at all: fall back to any position that at least
    # doesn't strand a particle or slice an English word
    n = len(remaining)
    for delta in range(0, 10):
        for cut in (target - delta, target + delta):
            if not _valid_cut(remaining, cut):
                continue
            if (remaining[cut].isascii() and remaining[cut].isalnum()
                    and remaining[cut - 1].isascii() and remaining[cut - 1].isalnum()):
                continue
            return cut
    return target


def split_text(text):
    """Break an over-long line into balanced, word-aligned pieces."""
    if len(text) <= MAX_CJK + 4:
        return [text]
    k = math.ceil(len(text) / MAX_CJK)
    target = round(len(text) / k)
    cut = find_cut(text, target)
    head, tail = text[:cut].strip(), text[cut:].strip()
    if not head or not tail:
        # find_cut degenerated; force a midpoint split rather than emit an
        # over-long line
        cut = target if 0 < target < len(text) else len(text) // 2
        head, tail = text[:cut].strip(), text[cut:].strip()
        if not head or not tail:
            return [text]
    # recurse on BOTH halves -- a cut can land off-centre, leaving a head that
    # is itself still too long
    return split_text(head) + split_text(tail)


# 3. Build CLAUSES from ASR tokens (ASR = base truth), applying overrides.
#    A clause is carved out by the script's punctuation *or* by the speaker
#    pausing; sentences are lists of clauses. Lines are assembled from clauses
#    in step 4 -- length never overrides a real boundary, it only subdivides.
sentences = []          # list of list of clause
cur_sentence = []       # list of (text, start, end, join_before)
cur_parts, cur_start, cur_end = [], None, None
cur_join = ''           # separator joining this clause to the previous one
next_join = ''          # ... to apply to the clause that starts next


def close_clause(join_next=''):
    """Close the clause in progress; `join_next` renders the boundary."""
    global cur_parts, cur_start, cur_end, cur_join, next_join
    if cur_parts:
        text = re.sub(r' {2,}', ' ', ''.join(cur_parts)).strip()
        if text:
            cur_sentence.append((text, cur_start, cur_end, cur_join))
    cur_parts, cur_start = [], None
    cur_join = next_join = join_next


def close_sentence():
    global cur_sentence
    close_clause()
    if cur_sentence:
        sentences.append(cur_sentence)
    cur_sentence = []


MOOD_PARTICLES = set('啊呢吧嘛呀哦噢')
pending_break = None
pending_join = SEP

for idx, (raw_text, s, e) in enumerate(asr_tokens):
    text = asr_override[idx] if asr_override[idx] is not None else raw_text

    if pending_break is not None:
        # A break fired on the previous token. If this token is a bare trailing
        # particle, it belongs to the sentence that just ended -- absorb it
        # rather than orphaning it at the head of the next line ("更有意思" +
        # "了"). The word-boundary test keeps this from splitting a word that
        # merely starts with one ("了解" must not become "了" | "解").
        join = '' if pending_break == 'pause' else pending_join
        if (text in CUT_AFTER and cur_parts
                and _tok_end[idx] in _word_ends):
            cur_parts.append(text)
            cur_end = e
            if pending_break == 'strong':
                close_sentence()
            else:
                close_clause(join)
            pending_break = None
            continue
        if pending_break == 'strong':
            close_sentence()
        else:
            close_clause(join)
        pending_break = None

    # A hesitation marker restarts the utterance -- never let it trail the
    # clause that just ended ("年营收是两个亿" + "呃这家公司啊").
    if asr_adlib[idx] and text and text[0] in HESITATION and cur_parts:
        close_clause(SEP)

    if cur_start is None:
        cur_start = s
    if cur_parts and WORD_RE.fullmatch(text) and cur_parts[-1] and re.fullmatch(r'[A-Za-z0-9]', cur_parts[-1][-1]):
        cur_parts.append(' ')
    cur_parts.append(text)
    cur_end = e

    brk = asr_break_after[idx]
    joined = ''.join(cur_parts).strip()
    # only a genuine leading word blocks a script break here; a trailing 的
    # ending the clause is fine (see the deferral note above)
    ends_badly = bool(joined) and joined[-1] in CUT_BEFORE
    if brk and not ends_badly:
        # 'strong' ends a sentence, 'weak' ends a clause within it
        pending_break = brk
        # A written comma marks a boundary in the *script's* wording. If the
        # speaker carried straight on into words the script doesn't have
        # ("第一件，" written, "第一件事是…" spoken), the comma no longer sits at
        # a boundary of what was actually said, so don't draw a gap there.
        nxt = idx + 1
        pending_join = '' if (nxt < n_tok and asr_adlib[nxt]) else SEP
    elif not ends_badly and idx + 1 < n_tok:
        # The speaker pauses where the meaning breaks, so a pause is a clause
        # boundary in its own right -- and the only one available wherever the
        # script's punctuation doesn't match the delivered wording. Rendered
        # without a visible gap: the space is how a written comma reads, while
        # this is simply a legal place to end a line.
        if asr_tokens[idx + 1][1] - e >= PAUSE_MS:
            pending_break = 'pause'
close_sentence()

# 4. Assemble lines: pack whole clauses onto a line, never splitting one just
#    to hit a length target. Only a clause that is itself too long gets cut.
LINE_TARGET = 14        # preferred width when packing clauses together
split_segments = []

for clauses in sentences:
    line = []           # list of clauses on the current line
    def emit(group):
        if not group:
            return
        text = group[0][0]
        for c in group[1:]:
            join = c[3]
            # A comma in the script can fall inside a word as delivered
            # ("第一件，" written, "第一件事" spoken). Rendering that boundary as
            # a gap splits the word on screen ("第一件 事呢"), so drop the space
            # when the two sides actually belong to one word.
            if join == SEP and text and _glued(text, c[0]):
                join = ''
            text += join + c[0]
        start, end = group[0][1], group[-1][2]
        pieces = split_text(text)
        if len(pieces) == 1:
            split_segments.append((start, end, pieces[0]))
            return
        dur, total = end - start, sum(len(p) for p in pieces) or 1
        t = start
        for i, p in enumerate(pieces):
            pe = end if i == len(pieces) - 1 else t + int(dur * len(p) / total)
            split_segments.append((t, pe, p))
            t = pe

    for clause in clauses:
        if not line:
            line = [clause]
            continue
        width = (sum(len(c[0]) for c in line)
                 + sum(len(c[3]) for c in line[1:])
                 + len(clause[3]) + len(clause[0]))
        if width <= LINE_TARGET:
            line.append(clause)
        else:
            emit(line)
            line = [clause]
    emit(line)

# 5. Absorb stray fragments (a stranded word or two) into a neighbouring line
MIN_STANDALONE = 4
final_segments = []
for i, (s, e, text) in enumerate(split_segments):
    # A short line is only a stray fragment if it runs on from a neighbour.
    # Where the speaker paused, it is a deliberate short utterance
    # ("…工作职责" [440ms] "然后呢") and merging destroys a boundary the script
    # and the audio agree on. Try backwards first, then FORWARDS: an
    # enumerator like "第七个" opens the clause that follows it, so it must
    # join the next line, never the one it was pushed off the end of.
    if len(text) < MIN_STANDALONE:
        if (final_segments and s - final_segments[-1][1] < PAUSE_MS
                and len(final_segments[-1][2]) + len(text) <= MAX_CJK + 4):
            ps, pe, pt = final_segments[-1]
            final_segments[-1] = (ps, e, (pt + text).strip())
            continue
        nxt = split_segments[i + 1] if i + 1 < len(split_segments) else None
        if (nxt and nxt[0] - e < PAUSE_MS
                and len(text) + 1 + len(nxt[2]) <= MAX_CJK + 4):
            split_segments[i + 1] = (s, nxt[1], f"{text}{SEP}{nxt[2]}")
            continue
    final_segments.append((s, e, text))

# The existing clause packer has a softer, historical width target. Apply the
# requested delivery format only after that segmentation, preserving all
# script/pause boundaries. This avoids re-packing short clauses merely because
# a vertical video has a narrower frame.
def display_width(s):
    """Chinese-character-equivalent width; fullwidth=1, ASCII=0.5."""
    return sum(1 if unicodedata.east_asian_width(c) in 'WF' else 0.5 for c in s)


def format_cut(text):
    """Choose a natural boundary within the hard width, never mid-word."""
    total = display_width(text)
    target = total / math.ceil(total / MAX_CHARS)
    leads = ('但是', '如果', '其实', '自己', '一起', '告诉', '哪个',
             '有没有', '反而', '专门', '要先', '是关于', '没有对比',
             '来完成', '来解决', '来给', '生成', '叫做', '我会', '做了')
    candidates = []
    for cut in word_boundaries(text):
        left, right = text[:cut].rstrip(), text[cut:].lstrip()
        if not left or not right or display_width(left) > MAX_CHARS:
            continue
        if left[-1] in STICKY or right[0] in STICKY | CUT_AFTER:
            continue
        if (left[-1].isascii() and left[-1].isalnum()
                and right[0].isascii() and right[0].isalnum()):
            continue
        semantic = next((w for w in leads if right.startswith(w)), None)
        bonus = 4 if semantic else 0
        if right.startswith(('但是', '如果', '自己', '其实', '哪个', '有没有')):
            bonus = 6
        if text[cut] == SEP or text[cut - 1] == SEP:
            bonus = max(bonus, 5)
        # Very short fragments are harder to read than mildly uneven lines.
        score = abs(display_width(left) - target) - bonus
        if display_width(left) < 4 or display_width(right) < 3:
            score += 5
        candidates.append((score, cut))
    if candidates:
        return min(candidates)[1]
    # Last resort for an unbroken token longer than the configured width.
    return max(1, next((i for i in range(1, len(text))
                        if display_width(text[:i + 1]) > MAX_CHARS), len(text) - 1))


def enforce_width(segments):
    out = []
    for start, end, text in segments:
        pieces = []
        while display_width(text) > MAX_CHARS:
            cut = format_cut(text)
            pieces.append(text[:cut].strip())
            text = text[cut:].strip()
        pieces.append(text)
        t = start
        total = sum(display_width(p) for p in pieces)
        for i, piece in enumerate(pieces):
            next_t = end if i == len(pieces) - 1 else start + round(
                (end - start) * sum(display_width(p) for p in pieces[:i + 1]) / total)
            out.append((t, next_t, piece))
            t = next_t
    return out


def repair_stranded_leads(segments):
    """Move a line-final standalone 给/用 to the phrase it introduces."""
    if MAX_CHARS > 18:
        return segments
    fixed = list(segments)
    for i in range(len(fixed) - 1):
        start, end, text = fixed[i]
        next_start, next_end, next_text = fixed[i + 1]
        if (not text or not next_text or next_start - end >= PAUSE_MS
                or jieba.lcut(text)[-1] not in {'给', '用'}):
            continue
        # Keep the join time close to the original word boundary. This is
        # only an estimate; as with other length cuts, review against audio.
        duration = max(1, end - start)
        shifted = min(180, max(40, round(duration / max(1, len(text)))))
        boundary = max(start, end - shifted)
        fixed[i] = (start, boundary, text[:-1].rstrip())
        fixed[i + 1] = (boundary, next_end, text[-1] + next_text)
    return fixed


def split_clear_clause_turns(segments):
    """Break compact lines only where an obvious new spoken clause begins."""
    out = []
    for start, end, text in segments:
        width = display_width(text)
        cuts = []
        if MAX_CHARS <= 18 and 14 <= width <= MAX_CHARS:
            for lead in ('我会', '要先', '告诉它', '或者是', '做了一'):
                at = text.find(lead, 1)
                if at > 0 and (display_width(text[:at]) >= 5
                               and display_width(text[at:]) >= 5):
                    cuts.append(at)
        if not cuts:
            out.append((start, end, text))
            continue
        cut = min(cuts, key=lambda at: abs(display_width(text[:at]) - width / 2))
        boundary = start + round((end - start) * display_width(text[:cut]) / width)
        out.extend(((start, boundary, text[:cut]), (boundary, end, text[cut:])))
    return out


final_segments = split_clear_clause_turns(enforce_width(repair_stranded_leads(final_segments)))

with open(OUT_PATH, 'w', encoding='utf-8') as f:
    for idx, (s, e, text) in enumerate(final_segments, 1):
        f.write(f"{idx}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{text}\n\n")

print(f"Wrote {len(final_segments)} segments to {OUT_PATH}", file=sys.stderr)

# 6. Optional review sheet: the places rules provably cannot settle, gathered
#    so a reader can judge them without re-reading the whole transcript.
if REVIEW_PATH:
    plain = ''.join(t for _, _, t in final_segments).replace(SEP, '')
    adlib_runs, run = [], []
    for k in range(n_tok):
        if asr_adlib[k] or asr_uncertain[k]:
            run.append(k)
        elif run:
            adlib_runs.append(run)
            run = []
    if run:
        adlib_runs.append(run)

    with open(REVIEW_PATH, 'w', encoding='utf-8') as f:
        f.write("# Review sheet\n\n"
                "Rules cannot settle these. Judge them by reading; see the\n"
                "skill's Known limits for why each category is here.\n")

        f.write("\n## 1. 的 / 地 / 得\n\n"
                "Adverbial (modifier + VERB) wants 地; complement (VERB + degree)\n"
                "wants 得; 地方/地区/天差地别 and 得(dei3) are already correct.\n\n")
        for pos, ch in ((i, c) for i, c in enumerate(plain) if c in PARTICLES):
            f.write(f"  {plain[max(0, pos - 12):pos]}【{ch}】{plain[pos + 1:pos + 9]}\n")

        # Words the subtitle uses that the script never does, split by how far
        # the reading has drifted. Phonetic distance separates the two reasons
        # the two texts can differ, and it is the single most useful sort order
        # here: near-misses are almost always the engine's fault, distant ones
        # almost always the speaker saying something else.
        script_plain = ''.join(a[0] for a in content_atoms)
        # Anchor each candidate to the script POSITIONALLY. Searching the whole
        # script for any near-homophone matches nonsense ("四周" -> "一直"):
        # what matters is only what the script says at this point in the talk.
        sub_to_script, anchors = {}, []
        for tag, a1, a2, b1, b2 in difflib.SequenceMatcher(
                None, plain, script_plain, autojunk=False).get_opcodes():
            if tag == 'equal':
                for k in range(a2 - a1):
                    sub_to_script[a1 + k] = b1 + k
                    anchors.append(a1 + k)

        def script_window(at, width):
            """The stretch of script aligned to `at`, plus a little slack."""
            prior = [p for p in anchors if p <= at]
            if not prior:
                return ''
            base = sub_to_script[prior[-1]] + (at - prior[-1])
            return script_plain[max(0, base - 4):base + width + 6]

        near_hits, far_hits, seen_words = [], [], set()
        cursor = 0
        for w in jieba.lcut(plain):
            here, cursor = cursor, cursor + len(w)
            if (len(w) < 2 or w in seen_words or w in script_plain
                    or not all(CJK_RE.match(c) for c in w)):
                continue
            seen_words.add(w)
            ctx = f"{plain[max(0, here - 12):here]}《{w}》{plain[cursor:cursor + 10]}"
            window = script_window(here, len(w))
            # compare against real words in that stretch of script, never an
            # arbitrary slice of it -- slices produce non-words ("析单", "调等")
            match = next((sw for sw in jieba.lcut(window)
                          if len(sw) == len(w) and sw != w and near_reading(w, sw)), None)
            (near_hits if match else far_hits).append((w, match, ctx))

        f.write("\n## 2a. Near-homophone of a word in the script -- LIKELY ASR ERROR\n\n"
                "Same syllable count, every syllable sharing an initial or a\n"
                "final. At that distance the engine mis-heard; the script's\n"
                "spelling should win. Confirm each in context before applying.\n\n")
        for w, match, ctx in near_hits:
            f.write(f"  {w} -> {match}?   {ctx}\n")

        f.write("\n## 2b. No close match in the script -- PROBABLY AS SPOKEN\n\n"
                "The reading is too far off to be a mis-hearing, so the speaker\n"
                "most likely said this. Judge from context; do not 'restore'\n"
                "the script's wording just because it differs.\n\n")
        for w, _, ctx in far_hits:
            f.write(f"  {w}   {ctx}\n")

        f.write("\n## 2c. Ad-libbed stretches in full\n\n")
        for r in adlib_runs:
            if len(r) < 2:
                continue
            said = ''.join(asr_tokens[k][0] for k in r)
            before = ''.join(asr_tokens[k][0] for k in range(max(0, r[0] - 8), r[0]))
            after = ''.join(asr_tokens[k][0]
                            for k in range(r[-1] + 1, min(n_tok, r[-1] + 9)))
            f.write(f"  {before}《{said}》{after}\n")

        f.write("\n## 3. Breaks with neither a pause nor punctuation behind them\n\n"
                "Chosen on length alone, so these are the least certain.\n\n")
        for i in range(len(final_segments) - 1):
            if final_segments[i + 1][0] - final_segments[i][1] < PAUSE_MS:
                f.write(f"  {final_segments[i][2]} ▌{final_segments[i + 1][2]}\n")

    print(f"Wrote review sheet to {REVIEW_PATH}", file=sys.stderr)
