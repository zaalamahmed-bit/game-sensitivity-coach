"""Verify the actual installed encoder with synthetic pixels, not the desktop."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from aim_observer.capture import VideoEncoder, find_ffmpeg, normalize_window_title


class EncoderTests(unittest.TestCase):
    def test_game_title_format_characters_do_not_break_foreground_matching(self):
        self.assertEqual(normalize_window_title("\u200bE\ufeffxam\u200bple Arena"), "example arena")

    def test_synthetic_video_has_one_decoded_frame_per_written_frame(self):
        try:
            ffmpeg = find_ffmpeg()
        except RuntimeError:
            self.skipTest("FFmpeg not installed")
        with tempfile.TemporaryDirectory() as directory:
            encoder = VideoEncoder(directory, 64, 48, 30, ffmpeg)
            try:
                for index in range(12):
                    encoder.write(bytes([0, 0, 80+index*10, 0]) * (64*48))
            finally:
                encoder.close()
            result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-i",
                                     str(Path(directory)/"video.mp4"), "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertEqual(len(result.stdout), 12*64*48*3)
            for index in range(12):
                offset = index*64*48*3
                r,g,b = result.stdout[offset:offset+3]
                self.assertLess(abs(r-(80+index*10)), 6)
                self.assertLess(g, 6)
                self.assertLess(b, 6)


if __name__ == "__main__":
    unittest.main()
