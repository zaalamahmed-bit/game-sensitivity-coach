import copy
import math
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from convert_sensitivity import convert


def example():
    def c(slope, offset=0):
        return dict(model='affine', slope=slope, offset=offset, min=0, max=100,
                    source='Synthetic fixture, not a real game calibration', evidence='measured')
    return dict(schema='game-sensitivity/conversion-request-v1', mode='turn_distance',
                source=dict(game='Synthetic A', value=10, dpi=800, calibration=c(.001)),
                target=dict(game='Synthetic B', dpi=800, calibration=c(.002)), target_decimals=10)


class ConversionTests(unittest.TestCase):
    def test_same_turn_distance_and_roundtrip(self):
        r = example(); d = convert(r)
        self.assertEqual(d['target_value'], 5)
        self.assertAlmostEqual(d['source_cm_per_360'], 114.3)
        self.assertAlmostEqual(d['target_cm_per_360'], 114.3)
        back = copy.deepcopy(r)
        back['source'], back['target'] = back['target'], back['source']
        back['source']['value'] = d['target_value']
        self.assertAlmostEqual(convert(back)['target_value'], 10)

    def test_unequal_dpi_and_offset(self):
        r = example(); r['target']['dpi'] = 1600
        r['target']['calibration']['offset'] = .001
        d = convert(r)
        self.assertEqual(d['target_value'], 2)
        self.assertAlmostEqual(d['target_cm_per_360'], d['source_cm_per_360'])

    def test_unknown_dpi_requires_explicit_assumption(self):
        r=example(); r['source']['dpi']=None
        with self.assertRaises(ValueError): convert(r)
        r['assume_same_dpi']=True
        d=convert(r)
        self.assertEqual(d['target_value'], 5)
        self.assertIsNone(d['source_cm_per_360'])
        self.assertIsNone(d['target_cm_per_360'])

    def test_conflicting_dpi(self):
        r=example(); r['assume_same_dpi']=True; r['target']['dpi']=1600
        with self.assertRaises(ValueError): convert(r)

    def test_monitor_distance_center_and_edge(self):
        r=example(); r['mode']='monitor_distance'
        r['projection']=dict(axis='horizontal',source_fov_degrees=90,target_fov_degrees=60,
                             fraction_of_half_screen=0,source='Synthetic optical fixture')
        self.assertAlmostEqual(convert(r)['target_value'],5/math.sqrt(3),places=8)
        r['projection']['fraction_of_half_screen']=1
        self.assertAlmostEqual(convert(r)['target_value'],5*2/3,places=8)

    def test_reject_unknown_and_invalid_calibration(self):
        for change in [{'slope':float('nan')},{'slope':0},{'source':''},{'model':'power'},
                       {'max':1},{'min':100},{'evidence':'guessed'}]:
            r=example(); r['source']['calibration'].update(change)
            with self.assertRaises(ValueError): convert(r)

    def test_range_not_silently_clamped(self):
        r=example(); r['target']['calibration']['max']=4
        with self.assertRaises(ValueError):convert(r)

    def test_conditional_result_and_provenance(self):
        r=example(); r['target']['calibration']['evidence']='assumed'
        d=convert(r)
        self.assertTrue(d['assumptions'])
        self.assertEqual(d['request'],r)
        self.assertEqual(len(d['request_sha256']),64)
        self.assertEqual(d['status'],'converted_estimate')


if __name__ == '__main__': unittest.main()
