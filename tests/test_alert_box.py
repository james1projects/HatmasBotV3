import asyncio, json, sys, tempfile
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from core import alert_box as AB  # noqa: E402
from core import sources as S  # noqa: E402

def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="alerts_test_"))

class _Overlay:
    def __init__(self):
        self.listeners = []; self.sent = []
    def add_event_listener(self, fn): self.listeners.append(fn)
    async def broadcast(self, name, action, data=None): self.sent.append((name, action, data))
    def client_count(self, name): return 0

def test_default_config_and_kinds():
    cfg = AB.default_config()
    assert 'boxes' in cfg
    assert 'main' in cfg['boxes']
    main_box = cfg['boxes']['main']
    assert 'kinds' in main_box
    kinds = main_box['kinds']
    assert 'bingo_open' in kinds
    assert 'bingo_claim' in kinds
    assert kinds['bingo_open']['enabled'] == True
    assert kinds['bingo_claim']['enabled'] == True
    assert kinds['gamble']['enabled'] == False
    assert kinds['tts']['enabled'] == False
    assert len(kinds) == len(AB.KINDS) and len(kinds) >= 15
    catalog = AB.kinds_catalog()
    assert isinstance(catalog, list)
    assert len(catalog) == len(AB.KINDS)
    for item in catalog:
        assert 'kind' in item
        assert 'label' in item
        assert 'events' in item
        assert 'legacy' in item
        assert 'defaults' in item
        assert 'sample' in item

def test_validate_config_clamps_and_fills():
    # Test with non-dict input -> defaults
    cfg = AB.validate_config("not a dict")
    assert isinstance(cfg, dict)
    assert 'boxes' in cfg
    assert 'main' in cfg['boxes']
    
    # Test invalid kinds dropped
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "nonexistent_kind": {"enabled": True}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert 'main' in cfg['boxes']
    main_kinds = cfg['boxes']['main']['kinds']
    assert 'nonexistent_kind' not in main_kinds
    
    # Test clamping duration
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"duration": 1000}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['duration'] == 600
    
    # Test clamping volume
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"volume": -10}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['volume'] == 0
    
    # Test clamping width
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"w": 2000}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['w'] == 1920
    
    # Test clamping height
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"h": 2000}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['h'] == 1080
    
    # Test clamping x
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"x": 2000, "w": 100}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['x'] == 1820
    
    # Test clamping y
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"y": 2000, "h": 100}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['y'] == 980
    
    # Test invalid anchor
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"anchor": "invalid"}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert cfg['boxes']['main']['kinds']['gamble']['anchor'] == AB.KINDS['gamble']['anchor'], 'bad anchor falls back to the kind default'
    
    # Test invalid box name
    raw = {
        "boxes": {
            "": {
                "kinds": {}
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert '' not in cfg['boxes']
    
    # Test lane validation and auto-creation
    raw = {
        "boxes": {
            "main": {
                "kinds": {
                    "gamble": {"lane": "invalid_lane_name"}
                }
            }
        }
    }
    cfg = AB.validate_config(raw)
    assert 'invalid_lane_name' in cfg['lanes']
    assert cfg['lanes']['invalid_lane_name']['max_queue'] == 10

def test_alert_box_writes_defaults_and_config_roundtrip():
    tmp = _tmp()
    path = tmp / "test_config.json"
    overlay = _Overlay()
    box = AB.AlertBox(overlay, str(path))
    
    # Check that file was written
    assert path.exists()
    
    # Read back config
    cfg = box.config()
    assert 'boxes' in cfg
    assert 'main' in cfg['boxes']
    
    # Modify and save
    cfg['boxes']['main']['kinds']['gamble']['enabled'] = True
    asyncio.run(box.save_config(cfg))
    
    # Create new box on same path
    box2 = AB.AlertBox(overlay, str(path))
    cfg2 = box2.config()
    assert cfg2['boxes']['main']['kinds']['gamble']['enabled'] == True

def test_on_event_respects_enabled_flags_and_boxes():
    tmp = _tmp()
    path = tmp / "test_config.json"
    overlay = _Overlay()
    box = AB.AlertBox(overlay, str(path))
    
    # Enable gamble in main
    cfg = box.config()
    cfg['boxes']['main']['kinds']['gamble']['enabled'] = True
    asyncio.run(box.save_config(cfg))
    
    # Add second box with gamble disabled
    cfg2 = box.config()
    cfg2['boxes']['second'] = {'kinds': {}}
    cfg2['boxes']['second']['kinds']['gamble'] = {
        'enabled': False,
        'lane': 'audio',
        'duration': 5,
        'sound': '',
        'volume': 100,
        'x': 0,
        'y': 0,
        'w': 400,
        'h': 200,
        'anchor': 'center'
    }
    asyncio.run(box.save_config(cfg2))
    
    # Emit gamble_result event
    asyncio.run(box.on_event('gamble_result', {}))
    
    # Should have exactly one alert broadcast, to main (save_config also pushed alerts_config messages)
    alerts = [x for x in overlay.sent if x[1] == 'alert']
    assert len(alerts) == 1, alerts
    name, action, data = alerts[0]
    assert name == 'alerts:main'
    assert action == 'alert'
    assert data['kind'] == 'gamble'

def test_test_replay_and_recent():
    tmp = _tmp()
    path = tmp / "test_config.json"
    overlay = _Overlay()
    box = AB.AlertBox(overlay, str(path))
    
    # Test a kind
    result = asyncio.run(box.test('gamble'))
    assert result['ok'] == True
    assert 'alert' in result
    alert = result['alert']
    assert alert['kind'] == 'gamble'
    assert alert['test'] == True
    
    # Test replay
    result = asyncio.run(box.replay(alert['id']))
    assert result['ok'] == True
    assert 'alert' in result
    assert result['alert']['kind'] == 'gamble'
    
    # Test recent
    recent = box.recent()
    assert len(recent) >= 1
    item = recent[0]
    assert 'id' in item
    assert 'kind' in item
    assert 'event' in item
    assert 'ts' in item
    assert 'test' in item
    assert 'summary' in item
    assert 'data' not in item

def test_sources_registry_and_readme():
    # Test registry with alert box
    tmp = _tmp()
    path = tmp / "test_config.json"
    overlay = _Overlay()
    box = AB.AlertBox(overlay, str(path))
    
    registry = S.registry(box, overlay)
    assert isinstance(registry, list)
    assert len(registry) > 0
    
    # Check for alert_box group entries
    alert_entries = [r for r in registry if r.get('group') == 'alert_box']
    assert len(alert_entries) >= 1
    
    # Test readme lines
    readme_lines = S.readme_lines("http://localhost:8069")
    assert isinstance(readme_lines, list)
    assert len(readme_lines) > 0
    assert any('Hatmaster Alert Box' in line for line in readme_lines)

def test_box_volume_and_background():
    tmp = _tmp()
    overlay = _Overlay()
    box = AB.AlertBox(overlay, str(tmp / "alerts.json"))
    cfg = box.config()
    assert cfg['boxes']['main']['volume'] == 100, "master volume defaults to 100"
    cfg['boxes']['main']['volume'] = 50
    cfg['boxes']['main']['kinds']['gamble']['volume'] = 80
    asyncio.run(box.save_config(cfg))
    assert box.box_config('main')['volume'] == 50
    res = asyncio.run(box.test('gamble'))
    a = res['alert']
    assert a['volume'] == 40 and a['kind_volume'] == 80 and a['box_volume'] == 50, a
    assert AB.validate_config({'boxes': {'main': {'volume': 500}}})['boxes']['main']['volume'] == 100, "clamped"
    # rolling feed kind exists and listens to both trade and dividend events
    assert 'tradefeed_rolling' in AB.AlertBox.kinds_for_event('trade_executed') and 'tradefeed_rolling' in AB.AlertBox.kinds_for_event('dividend_paid')
    assert 'feed' in cfg['lanes']
    # scene screenshot
    assert box.background_path() is None and box.status()['background'] is False
    assert not box.set_background(b'', 'png')['ok']
    assert not box.set_background(b'x' * 10, 'gif')['ok'], "gif refused"
    r = box.set_background(b'PNG fake bytes', 'PNG')
    assert r['ok'] and r['ext'] == 'png' and box.background_path().name == 'alerts_background.png'
    r = box.set_background(b'JPEG fake bytes', 'jpeg')
    assert r['ok'] and box.background_path().name == 'alerts_background.jpg', "one background at a time, jpeg -> jpg"
    assert box.status()['background'] is True
    assert box.clear_background()['removed'] == 1 and box.background_path() is None


def test_shared_lane_placement():
    tmp = _tmp()
    box = AB.AlertBox(_Overlay(), str(tmp / "alerts.json"))
    cfg = box.config()
    lane = cfg['lanes']['economy']
    assert lane['shared'] is False and set(lane) >= {'max_queue', 'x', 'y', 'w', 'h', 'anchor'}, lane
    # off: the kind's own placement
    a = asyncio.run(box.test('portfolio'))['alert']
    assert a['shared_lane'] is False and a['placement']['x'] == AB.KINDS['portfolio']['x']
    # on: every kind in the lane lands in the lane's spot
    cfg['lanes']['economy'].update({'shared': True, 'x': 100, 'y': 200, 'w': 600, 'h': 300, 'anchor': 'top-left'})
    asyncio.run(box.save_config(cfg))
    for kind in ('portfolio', 'match_end'):
        a = asyncio.run(box.test(kind))['alert']
        assert a['shared_lane'] is True and a['placement'] == {'x': 100, 'y': 200, 'w': 600, 'h': 300, 'anchor': 'top-left'}, a
    # a kind in another lane is untouched; clamping applies to lane placements too
    a = asyncio.run(box.test('gamble'))['alert']
    assert a['shared_lane'] is False and a['placement']['x'] == AB.KINDS['gamble']['x']
    v = AB.validate_config({'lanes': {'economy': {'shared': 1, 'x': 5000, 'w': 99999, 'anchor': 'nope'}}})['lanes']['economy']
    assert v['shared'] is True and v['w'] == 1920 and v['x'] == 0 and v['anchor'] == 'center'
    # unknown lane referenced by a kind gets full defaults
    v = AB.validate_config({'boxes': {'main': {'kinds': {'gamble': {'lane': 'popups'}}}}})
    assert v['lanes']['popups']['shared'] is False and v['lanes']['popups']['w'] == 520


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

def main() -> int:
    passed = failed = 0
    for t in TESTS:
        try:
            t(); print(f"PASS  {t.__name__}"); passed += 1
        except AssertionError as e:
            print(f"FAIL  {t.__name__}: {e}"); failed += 1
        except Exception as e:
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}"); failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if not TESTS:
        print("FAIL  no tests were collected"); return 1
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())
