"""Score a generated SRT's line breaks against a hand-corrected reference.

usage: eval_breaks.py <reference.srt> <candidate.srt>

Reports recall / precision / F1 over break positions, plus the text
similarity, and lists the breaks each side has that the other lacks. Use it to
decide whether a segmentation rule change actually helps -- several changes
that sounded obviously right measured worse.
"""
import re
import sys
import difflib


def load(path):
    text = open(path, encoding='utf-8').read()
    out = []
    for block in re.split(r'\n\s*\n', text.strip()):
        lines = block.strip().splitlines()
        if len(lines) >= 3:
            out.append(' '.join(lines[2:]).strip())
    return out


def flat(segs):
    s, brk = '', []
    for t in segs:
        s += t.replace(' ', '')
        brk.append(len(s))
    return s, brk[:-1]


def compare(ref_path, cand_path, show=12):
    ref, cand = load(ref_path), load(cand_path)
    tr, br = flat(ref)
    tc, bc = flat(cand)
    sm = difflib.SequenceMatcher(None, tc, tr, autojunk=False)
    c2r = {}
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            for k in range(i2 - i1):
                c2r[j1 + k] = i1 + k
    ref_breaks = {c2r[b] for b in br if b in c2r}
    cand_breaks = set(bc)
    hit = len(ref_breaks & cand_breaks)
    recall = hit / max(len(ref_breaks), 1)
    prec = hit / max(len(cand_breaks), 1)
    f1 = 2 * recall * prec / max(recall + prec, 1e-9)
    print(f"segments: reference {len(ref)}, candidate {len(cand)}")
    print(f"recall {recall:.1%}  precision {prec:.1%}  F1 {f1:.1%}  text similarity {sm.ratio():.4f}")
    missed = sorted(ref_breaks - cand_breaks)
    spurious = sorted(cand_breaks - ref_breaks)
    print(f"\nmissed {len(missed)} (reference breaks here, candidate does not):")
    for b in missed[:show]:
        print(f"   ...{tc[max(0, b - 14):b]} | {tc[b:b + 12]}...")
    print(f"\nspurious {len(spurious)} (candidate breaks here, reference does not):")
    for b in spurious[:show]:
        print(f"   ...{tc[max(0, b - 14):b]} | {tc[b:b + 12]}...")
    return f1


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    compare(sys.argv[1], sys.argv[2])
