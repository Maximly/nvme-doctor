from src.diff import compare_reports, render_diff_text


def report(unsafe, cycles, media=0, errors=0, speed='16.0 GT/s PCIe'):
    return {
        'status': 'OK',
        'snapshot': {
            'collected_at': '2026-08-09T00:00:00+00:00',
            'device_path': '/dev/nvme4',
            'smart': {
                'unsafe_shutdowns': unsafe,
                'power_cycles': cycles,
                'media_errors': media,
                'num_err_log_entries': errors,
                'data_units_read': 100,
                'data_units_written': 200,
            },
            'pci': {'current_link_speed': speed, 'current_link_width': '4'},
        },
        'findings': [],
    }


def test_diff_detects_unsafe_shutdown_increment():
    d = compare_reports(report(50, 61), report(51, 62))
    unsafe = next(x for x in d['changes'] if x['label'] == 'Unsafe shutdowns')
    assert unsafe['delta'] == 1
    assert any('Unsafe shutdown occurred' in x['title'] for x in d['interpretations'])
    text = render_diff_text(d)
    assert 'Unsafe shutdowns' in text
    assert '+1' in text
    assert 'does not record the individual reason or timestamp' in text


def test_diff_warns_when_pcie_generation_drops():
    d = compare_reports(report(0, 1, speed='32.0 GT/s PCIe'), report(0, 1, speed='16.0 GT/s PCIe'))
    item = next(x for x in d['interpretations'] if x['title'] == 'PCIe generation changed')
    assert item['severity'] == 'warning'
    assert 'Gen5 to Gen4' in item['detail']
