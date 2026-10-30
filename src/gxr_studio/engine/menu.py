"""Strict UI-level to integer-request adapter using the final exported JSON."""
import json
from pathlib import Path
from . import knobs as k

TABLE_PATH = Path(__file__).resolve().parent / 'data/final.json'


def load():
    return json.loads(TABLE_PATH.read_text())


def request(data, unit, slot, levels):
    if type(unit) is not int or type(slot) is not int or not 0 <= unit < 3 or not 1 <= slot <= 8:
        raise ValueError('unit/slot')
    recipe = data['recipes'][unit * 8 + slot - 1]
    items = data['items'][recipe['first_item']:recipe['first_item'] + recipe['item_count']]
    if len(levels) != len(items):
        raise ValueError('menu item count')
    amount, doses = 100, [0] * 10
    for item, level in zip(items, levels):
        if type(level) is not int:
            raise ValueError('integer UI level required')
        point = next((p for p in item['points'] if p['level'] == level), None)
        if point is None:
            raise ValueError('unlisted or capped UI level')
        control = data['controls'][item['control']]
        if control['dose_index'] < 0:
            amount = point['dose']
        else:
            doses[control['dose_index']] = point['dose']
    return k.Request(amount, tuple(doses))
