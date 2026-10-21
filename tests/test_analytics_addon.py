from core import analytics_addon as addon


def test_numeric_csv():
    rows = addon._parse_delimited('Дата;Расход, ₽;Клики\n2026-10-20;916,71;12')
    assert rows[0]['Расход, ₽'] == 916.71
    assert rows[0]['Клики'] == 12


def test_metrics_mismatch_preserves_raw():
    rows, warning = addon.map_analytics_rows([{'metrics': [10]}], ['day'], ['a', 'b'])
    assert rows == [{'metrics_raw': [10]}]
    assert warning


def test_history_isolated_by_cabinet(tmp_path, monkeypatch):
    monkeypatch.setattr(addon, 'DB_PATH', str(tmp_path / 'history.sqlite'))
    credentials = {'ozon_client_id': 'shop-one'}
    monkeypatch.setattr(addon, 'get_credentials', lambda: credentials)
    addon.save_snapshot('2026-10-20', 'ozon', 'prices', [{'id': 1}], lambda row: row['id'])
    import asyncio
    assert asyncio.run(addon.history_get('ozon', 'prices', '2026-10-20', '2026-10-20'))['count'] == 1
    credentials['ozon_client_id'] = 'shop-two'
    assert asyncio.run(addon.history_get('ozon', 'prices', '2026-10-20', '2026-10-20'))['count'] == 0
