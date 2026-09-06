import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from discover_settings import discover


class DiscoveryTests(unittest.TestCase):
    def test_bounded_explicit_root_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); p=root/'settings'; p.mkdir()
            f=p/'PROFSAVE_profile'; f.write_bytes(b'GstInput.MouseSensitivity 0.01\r\n')
            (root/'video.mp4').write_bytes(b'not a config')
            d=discover('Example Game',[root])
            self.assertEqual([x['path'] for x in d['candidates']],[str(f.resolve())])
            self.assertEqual(f.read_bytes(),b'GstInput.MouseSensitivity 0.01\r\n')

    def test_reports_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            for i in range(8):(Path(tmp)/(str(i)+'.ini')).write_text('x=1')
            d=discover('Example',[tmp],max_files=3)
            self.assertTrue(d['truncated'])
            self.assertLess(len(d['candidates']),8)


if __name__=='__main__':unittest.main()
