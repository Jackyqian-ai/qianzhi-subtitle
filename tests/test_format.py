"""Small end-to-end checks for format limits and content preservation."""
import re
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALIGN = ROOT / 'scripts' / 'align_subs.py'


def width(text):
    return sum(1 if unicodedata.east_asian_width(c) in 'WF' else .5 for c in text)


def cues(path):
    return [block.splitlines()[2] for block in re.split(r'\n\s*\n',
            path.read_text(encoding='utf-8').strip())]


class FormatTest(unittest.TestCase):
    def test_vertical_limit_and_horizontal_regression(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            phrase = '最近啊我用ChatGPT呢给自己的新课程做了一张宣传海报'
            words = re.findall(r'ChatGPT|.', phrase)
            raw = tmp / 'raw.srt'
            raw.write_text(''.join(
                f'{i}\n00:00:{(i-1)//10:02d},{((i-1)%10)*100:03d} --> '
                f'00:00:{i//10:02d},{(i%10)*100:03d}\n{word}\n\n'
                for i, word in enumerate(words, 1)), encoding='utf-8')
            script = tmp / 'script.txt'
            script.write_text('最近啊我用ChatGPT呢，给自己的新课程做了一张宣传海报。',
                              encoding='utf-8')

            def run(name, *options):
                out = tmp / name
                subprocess.run([sys.executable, str(ALIGN), str(raw), str(script),
                                str(out), *options], check=True, capture_output=True)
                return out

            vertical = cues(run('vertical.srt', '--max-chars', '16'))
            horizontal = cues(run('horizontal.srt', '--max-chars', '24'))
            default = cues(run('default.srt'))
            self.assertTrue(all(width(text) <= 16 for text in vertical))
            self.assertEqual(''.join(vertical).replace(' ', ''),
                             ''.join(horizontal).replace(' ', ''))
            self.assertEqual(default, horizontal)
            self.assertGreater(len(vertical), len(horizontal))

    def test_rejects_impossible_tiny_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(ALIGN), 'a', 'b', 'c',
                                     '--max-chars', '3'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'between 4 and 80', result.stderr)


if __name__ == '__main__':
    unittest.main()
