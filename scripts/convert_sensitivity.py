"""Convert verified game calibrations; no built-in game constants. Python 3.8+."""
import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + ' must be a finite number')
    if positive and value <= 0:
        raise ValueError(name + ' must be positive')
    return float(value)


def calibration(view, name):
    c = view.get('calibration', {})
    if c.get('model') != 'affine':
        raise ValueError(name + ': only explicitly calibrated affine mappings are supported')
    slope = number(c.get('slope'), name + '.calibration.slope', True)
    offset = number(c.get('offset'), name + '.calibration.offset')
    lo = number(c.get('min'), name + '.calibration.min')
    hi = number(c.get('max'), name + '.calibration.max')
    if lo >= hi:
        raise ValueError(name + ': invalid calibration range')
    if not isinstance(c.get('source'), str) or not c['source'].strip():
        raise ValueError(name + ': calibration source is required')
    if c.get('evidence') not in ('measured', 'documented', 'assumed'):
        raise ValueError(name + ': evidence must be measured, documented or assumed')
    return slope, offset, lo, hi


def convert(request):
    if request.get('schema') != 'game-sensitivity/conversion-request-v1':
        raise ValueError('Unsupported conversion request schema')
    mode = request.get('mode')
    if mode not in ('turn_distance', 'monitor_distance'):
        raise ValueError('mode must be turn_distance or monitor_distance')
    source, target = request.get('source', {}), request.get('target', {})
    a, b, lo, hi = calibration(source, 'source')
    ta, tb, tlo, thi = calibration(target, 'target')
    value = number(source.get('value'), 'source.value')
    if not lo <= value <= hi:
        raise ValueError('Source value is outside the calibrated range')
    source_gain = a * value + b
    if source_gain <= 0:
        raise ValueError('Source calibration gives a nonpositive angular gain')
    sdpi, tdpi = source.get('dpi'), target.get('dpi')
    if sdpi is not None:
        sdpi = number(sdpi, 'source.dpi', True)
    if tdpi is not None:
        tdpi = number(tdpi, 'target.dpi', True)
    same = request.get('assume_same_dpi', False)
    if not isinstance(same, bool):
        raise ValueError('assume_same_dpi must be boolean')
    assumptions = []
    if sdpi is None or tdpi is None:
        if not same:
            raise ValueError('DPI is unknown: supply both values or explicitly assume_same_dpi')
        dpi_ratio = 1.0
        assumptions.append('Same DPI across both games is assumed; physical cm/360 is unknown.')
    else:
        if same and sdpi != tdpi:
            raise ValueError('Same-DPI assumption contradicts the supplied DPI values')
        dpi_ratio = sdpi / tdpi
    match_ratio = 1.0
    optical = None
    if mode == 'monitor_distance':
        optical = request.get('projection', {})
        if optical.get('axis') not in ('horizontal', 'vertical'):
            raise ValueError('Projection must specify a shared horizontal or vertical axis')
        sf = number(optical.get('source_fov_degrees'), 'source FOV', True)
        tf = number(optical.get('target_fov_degrees'), 'target FOV', True)
        radius = number(optical.get('fraction_of_half_screen'), 'screen fraction')
        if sf >= 179 or tf >= 179 or not 0 <= radius <= 1:
            raise ValueError('Require rectilinear FOV below 179 degrees and screen fraction in [0,1]')
        if not isinstance(optical.get('source'), str) or not optical['source'].strip():
            raise ValueError('Projection/FOV evidence source is required')
        st = math.tan(math.radians(sf) / 2)
        tt = math.tan(math.radians(tf) / 2)
        match_ratio = tt / st if radius == 0 else math.atan(radius * tt) / math.atan(radius * st)
        assumptions.append('Matches a single rectilinear view and screen radius, not every optic or displacement.')
    required_gain = source_gain * dpi_ratio * match_ratio
    unrounded = (required_gain - tb) / ta
    if not math.isfinite(unrounded) or not tlo <= unrounded <= thi:
        raise ValueError('Converted target is outside its calibrated range; no clamping was applied')
    decimals = request.get('target_decimals', 6)
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 12:
        raise ValueError('target_decimals must be an integer from 0 to 12')
    rounded = round(unrounded, decimals)
    if not tlo <= rounded <= thi:
        raise ValueError('Rounded target would leave its calibrated range')
    actual_gain = ta * rounded + tb
    if actual_gain <= 0:
        raise ValueError('Rounded target has a nonpositive angular gain')
    for name, view in [('source', source), ('target', target)]:
        if view['calibration']['evidence'] == 'assumed':
            assumptions.append(name + ' calibration is assumed; validate it before applying.')
    both_known = sdpi is not None and tdpi is not None
    canonical = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {
        'schema': 'game-sensitivity/conversion-result-v1',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'converted_estimate', 'mode': mode,
        'source_gain_degrees_per_count': source_gain,
        'required_target_gain_degrees_per_count': required_gain,
        'target_value_unrounded': unrounded, 'target_value': rounded,
        'target_gain_degrees_per_count': actual_gain,
        'rounding_gain_error_percent': (actual_gain / required_gain - 1) * 100,
        'source_cm_per_360': 914.4 / (sdpi * source_gain) if both_known else None,
        'target_cm_per_360': 914.4 / (tdpi * actual_gain) if both_known else None,
        'assumptions': assumptions,
        'note': 'Conversion preserves the chosen metric under supplied calibration; it does not optimize aim or verify game settings.',
        'request_sha256': hashlib.sha256(canonical).hexdigest(), 'request': request,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('request', type=Path, help='Agent-authored conversion request JSON')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    try:
        result = convert(json.loads(args.request.read_text(encoding='utf-8-sig')))
        text = json.dumps(result, indent=2, allow_nan=False) + '\n'
        if args.output:
            # Each result is a new audit artifact, never silently overwrite one.
            with args.output.open('x', encoding='utf-8') as stream:
                stream.write(text)
        else:
            print(text, end='')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        p.exit(2, 'Conversion failed: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
