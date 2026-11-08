"""Import the generator without the local recall / battery modules."""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def install_stubs():
    def mod(name):
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    recall = mod('recall_data')

    def load_db():
        raise OSError('no recall db in tests')

    recall.load_db = load_db
    recall.load_vin_results = lambda: {}
    recall.load_pending = lambda: []
    recall.COLS = []
    recall.campaign_ids = lambda text: []
    common = mod('vf_common')
    common.distance_from_home = lambda *args, **kwargs: ''
    battery = mod('vf_battery')
    battery.battery_type = lambda *args, **kwargs: ''
    battery.drivetrain = lambda vin, dt, *args, **kwargs: dt or ''


def load_build_app():
    install_stubs()
    sys.path.insert(0, str(ROOT))
    import build_app
    return build_app
