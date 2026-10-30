"""Fault-path tests: never connect to or alter the real game."""
import struct
import threading
import unittest
from unittest.mock import patch
import tempfile
from pathlib import Path
import json
import runtime_paths

from core import Game, DataWriteError
from errors import LOG, ROLLBACK


class Fake(Game):
    def __init__(self):
        self.lock = threading.RLock()
        self.undo = {}
        self.auto_shachi = None
        self.auto_spirit_shadow = None
        self.auto_health = None
        self.auto_medicines = {}
        self.auto_ultimate = None
        self.memory = struct.pack('<ff', 100, 100)
        self.identity = (1000, 4, 2000, 3000)
        self.writes = []
        self.logs = []
        self.change_during_log = False

    def targets(self, feature):
        return [(1000, 1100, '<ff')]

    def token(self, owner):
        return self.identity

    def read(self, address, size):
        return self.memory

    def raw_write(self, address, payload):
        self.writes.append(payload)
        self.memory = payload

    def alive(self):
        return True

    def log(self, row):
        self.logs.append(row)
        if self.change_during_log and row['action'] == 'write_intent':
            self.identity = (1000, 5, 2000, 3000)


class Safeguards(unittest.TestCase):
    def test_frozen_action_log_survives_outside_bundled_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            g = Fake()
            g.pid = 123
            with patch.object(runtime_paths.sys, 'frozen', True, create=True), \
                    patch.dict(runtime_paths.os.environ, {'LOCALAPPDATA': directory}):
                Game.log(g, {'action': 'fixture_no_game_write'})
                log = Path(directory) / 'PBZTrainer' / 'logs' / 'actions.jsonl'
                self.assertTrue(log.is_file())
                self.assertNotEqual(log.parent.parent, runtime_paths.RESOURCE_HOME)
                self.assertEqual(json.loads(log.read_text(encoding='utf8'))['action'], 'fixture_no_game_write')
                self.assertEqual(g.writes, [])
            self.assertTrue(log.is_file())

    @classmethod
    def movement_game(cls):
        g, memory = cls.health_game()
        g.targets = lambda feature: Game.targets(g, feature)
        for offset, value in ((0xd88, 130), (0xd98, 600), (0xda8, 750), (0xdb8, 300)):
            struct.pack_into('<ff', memory, 1000 + offset + 8, value, value)
        return g, memory

    def test_movement_group_scales_inputs_only_and_reapply_does_not_compound(self):
        g, memory = self.movement_game()
        before = bytes(memory)
        records = g.modifier('movement', 1.5)
        self.assertEqual(len(records), 4)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xda8 + 8), (1125, 1125))
        for index, value in enumerate(before):
            if not any(1000 + offset + 8 <= index < 1000 + offset + 16 for offset in (0xd88, 0xd98, 0xda8, 0xdb8)):
                self.assertEqual(memory[index], value)
        g.modifier('movement', 2)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd98 + 8), (1200, 1200))
        g.modifier('movement', 1)
        self.assertEqual(bytes(memory), before)
        self.assertEqual(g.undo, {})

    def test_movement_rejects_bad_input_and_invalid_source_before_writes(self):
        g, memory = self.movement_game()
        for value in (0.9, 5.1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                g.modifier('movement', value)
        struct.pack_into('<f', memory, 1000 + 0xda8 + 12, 0)
        with self.assertRaises(RuntimeError):
            g.modifier('movement', 1.5)
        self.assertEqual(g.writes, [])

    def test_movement_partial_failure_restores_completed_fields(self):
        g, memory = self.movement_game()
        before, normal = bytes(memory), g.raw_write
        def write(address, payload):
            if address == 1000 + 0xda8 + 8:
                raise DataWriteError('sprint write failed', 0)
            normal(address, payload)
        g.raw_write = write
        with self.assertRaises(DataWriteError):
            g.modifier('movement', 1.5)
        self.assertEqual(bytes(memory), before)
        self.assertEqual(g.undo, {})

    def test_movement_restore_keeps_game_change_and_does_not_touch_health_lock(self):
        g, memory = self.movement_game()
        g.modifier('movement', 1.5)
        g.undo['health_floor'] = {'sentinel': True}
        struct.pack_into('<ff', memory, 1000 + 0xd98 + 8, 630, 630)
        results = g.restore_movement()
        self.assertEqual(set(g.undo), {'movement:JogSpeed', 'health_floor'})
        self.assertIn('movement:JogSpeed', results)
        count = len(g.writes)
        with self.assertRaises(RuntimeError):
            g.modifier('movement', 2)
        self.assertEqual(len(g.writes), count)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd98 + 8), (630, 630))

    def test_movement_rejects_identity_change_during_write_intent(self):
        g, memory = self.movement_game()
        before = bytes(memory)
        g.change_during_log = True
        with self.assertRaises(RuntimeError):
            g.modifier('movement', 1.5)
        self.assertEqual(bytes(memory), before)
        self.assertEqual(g.writes, [])

    def test_movement_partial_restore_cleans_boosted_base_preserving_game_current(self):
        for current in (750, 950):
            g, memory = self.movement_game()
            g.modifier('movement', 1.5)
            struct.pack_into('<f', memory, 1000 + 0xda8 + 12, current)
            results = g.restore_movement()
            self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xda8 + 8), (750, current))
            self.assertEqual(g.undo, {})
            self.assertIn('movement:SprintSpeed', results)

    @classmethod
    def dust_game(cls):
        g, memory = cls.health_game()
        context = {'attributes': 1000, 'pawn': 3000}
        g.context = lambda: dict(context)
        g.token = lambda pointer: (pointer, 4, 2000, 9000)
        g.targets = lambda feature: Game.targets(g, feature)
        struct.pack_into('<i', memory, 3000 + 0x1040, 40460)
        return g, memory, context

    def test_dust_only_writes_four_bytes_and_can_undo(self):
        g, memory, _ = self.dust_game()
        before = bytes(memory)
        state = g.dust_state()
        record = g.set_dust(2000000000, expected_token=state['token'])
        self.assertEqual(g.dust_state()['amount'], 2000000000)
        self.assertEqual(len(g.writes[0]), 4)
        self.assertEqual(bytes(memory[:7160]), before[:7160])
        self.assertEqual(bytes(memory[7164:]), before[7164:])
        self.assertEqual(g.undo, {})  # One-shot edits are not restored on panel exit.
        g.restore_record(record)
        self.assertEqual(bytes(memory), before)

    def test_dust_rejects_noninteger_out_of_range_and_stale_selection(self):
        g, memory, _ = self.dust_game()
        for value in (True, 1.5, '10', -1, 2000000001):
            with self.assertRaises(ValueError):
                g.set_dust(value)
        with self.assertRaises(RuntimeError):
            g.set_dust(10, expected_token=(3000, 99, 2000, 9000))
        struct.pack_into('<i', memory, 7160, -1)
        with self.assertRaises(RuntimeError):
            g.set_dust(10)
        self.assertEqual(g.writes, [])

    def test_dust_player_change_during_intent_does_not_write_old_pawn(self):
        g, _, context = self.dust_game()
        original_log = g.log
        def log(row):
            original_log(row)
            if row['action'] == 'write_intent':
                context['pawn'] = 4000
        g.log = log
        with self.assertRaises(RuntimeError):
            g.set_dust(100)
        self.assertEqual(g.writes, [])

    def test_dust_undo_preserves_game_gains_and_previous_pawn(self):
        for change in ('gain', 'pawn', 'gain_in_log', 'pawn_in_log'):
            g, memory, context = self.dust_game()
            record = g.set_dust(100)
            if change == 'gain':
                struct.pack_into('<i', memory, 7160, 101)
            elif change == 'pawn':
                context['pawn'] = 4000
            else:
                original_log = g.log
                def log(row):
                    original_log(row)
                    if row['action'] == 'restore_intent':
                        if change == 'gain_in_log':
                            struct.pack_into('<i', memory, 7160, 101)
                        else:
                            context['pawn'] = 4000
                g.log = log
            with self.assertRaises(RuntimeError):
                g.restore_record(record)
            self.assertEqual(len(g.writes), 1)
            self.assertEqual(struct.unpack_from('<i', memory, 7160)[0], 101 if change.startswith('gain') else 100)

    def test_dark_source_lock_refills_only_consumed_count_and_preserves_capacity(self):
        g, memory = self.health_game()
        g.targets = lambda feature: Game.targets(g, feature)
        struct.pack_into('<ff', memory, 1000 + 0x900, 3, 3)
        struct.pack_into('<ff', memory, 1000 + 0x910, 4, 4)
        before_max = bytes(memory[1000 + 0x908:1000 + 0x918])
        g.start_auto_spirit_shadow()
        self.assertEqual(g.writes, [])
        self.assertTrue(g.pulse_auto_spirit_shadow())
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x900), (4, 4))
        self.assertEqual(bytes(memory[1000 + 0x908:1000 + 0x918]), before_max)
        self.assertFalse(g.pulse_auto_spirit_shadow())
        g.stop_auto_spirit_shadow()
        struct.pack_into('<ff', memory, 1000 + 0x900, 2, 2)
        self.assertFalse(g.pulse_auto_spirit_shadow())
        self.assertEqual(len(g.writes), 1)

    def test_dark_source_lock_stops_on_identity_invalid_values_or_log_race(self):
        for change in ('identity', 'capacity', 'nan', 'intent'):
            g, memory = self.health_game()
            g.targets = lambda feature: Game.targets(g, feature)
            struct.pack_into('<ff', memory, 1000 + 0x900, 3, 3)
            struct.pack_into('<ff', memory, 1000 + 0x910, 4, 4)
            g.start_auto_spirit_shadow()
            if change == 'identity':
                g.identity = (1000, 99, 2000, 3000)
            elif change == 'capacity':
                struct.pack_into('<f', memory, 1000 + 0x914, 0)
            elif change == 'nan':
                struct.pack_into('<f', memory, 1000 + 0x904, float('nan'))
            else:
                g.change_during_log = True
            with self.assertRaises(RuntimeError):
                g.pulse_auto_spirit_shadow()
            self.assertIsNone(g.auto_spirit_shadow)
            self.assertEqual(g.writes, [])

    @classmethod
    def resource_game(cls):
        g, memory = cls.health_game()
        for pointer in (5000, 6000):
            struct.pack_into('<iii', memory, pointer + 0x50, 2, 2, 2)
        struct.pack_into('<i', memory, 3000 + 0x670, 1)
        for offset, value in [(0x898, 25), (0x8a8, 100), (0x8b8, 0), (0x8c8, 1)]:
            struct.pack_into('<ff', memory, 1000 + offset + 8, value, value)
        g.medicine = lambda context, kind='Hulu': (3000, {'Hulu': 5000, 'FeiJiu': 6000}[kind])
        g.token = lambda pointer: g.identity if pointer == 1000 else (pointer, 4, 2000, 3000)
        g.targets = lambda feature: Game.targets(g, feature)
        return g, memory

    def test_medicine_locks_are_independent_and_only_refill_used_counts(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('Hulu')
        g.start_auto_medicine('FeiJiu')
        self.assertEqual(g.writes, [])
        before_max = bytes(memory[5000 + 0x58:5000 + 0x60])
        self.assertTrue(g.pulse_auto_medicine('Hulu'))
        self.assertEqual(struct.unpack_from('<ii', memory, 5000 + 0x50), (3, 3))
        self.assertEqual(struct.unpack_from('<ii', memory, 6000 + 0x50), (2, 2))
        self.assertFalse(g.pulse_auto_medicine('Hulu'))
        g.stop_auto_medicine('Hulu')
        struct.pack_into('<ii', memory, 5000 + 0x50, 1, 1)
        self.assertFalse(g.pulse_auto_medicine('Hulu'))
        self.assertTrue(g.pulse_auto_medicine('FeiJiu'))
        self.assertEqual(struct.unpack_from('<ii', memory, 6000 + 0x50), (3, 3))
        self.assertEqual(bytes(memory[5000 + 0x58:5000 + 0x60]), before_max)

    def test_medicine_lock_stops_on_player_change_without_writes(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('FeiJiu')
        g.identity = (1000, 9, 2000, 3000)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_medicine('FeiJiu')
        self.assertEqual(g.writes, [])
        self.assertNotIn('FeiJiu', g.auto_medicines)

    def test_medicine_lock_rejects_replaced_medicine_and_keeps_other_lock(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('Hulu')
        g.start_auto_medicine('FeiJiu')
        struct.pack_into('<iii', memory, 6100 + 0x50, 1, 1, 2)
        g.medicine = lambda context, kind='Hulu': (3000, 5000 if kind == 'Hulu' else 6100)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_medicine('FeiJiu')
        self.assertEqual(g.writes, [])
        self.assertEqual(set(g.auto_medicines), {'Hulu'})

    def test_medicine_player_change_during_intent_log_blocks_write(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('FeiJiu')
        normal = g.log
        def log(row):
            normal(row)
            if row['action'] == 'write_intent':
                g.identity = (1000, 99, 2000, 3000)
        g.log = log
        with self.assertRaises(RuntimeError):
            g.pulse_auto_medicine('FeiJiu')
        self.assertEqual(g.writes, [])
        self.assertEqual(struct.unpack_from('<ii', memory, 6000 + 0x50), (2, 2))

    def test_medicine_invalid_capacity_stops_and_stop_log_failure_clears_state(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('FeiJiu')
        struct.pack_into('<i', memory, 6000 + 0x58, 0)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_medicine('FeiJiu')
        self.assertEqual(g.writes, [])
        g.start_auto_medicine('Hulu')
        g.log = lambda row: (_ for _ in ()).throw(OSError('disk full'))
        g.stop_auto_medicine()
        self.assertEqual(g.auto_medicines, {})

    def test_ultimate_lock_refills_both_attributes_without_changing_capacities(self):
        g, memory = self.resource_game()
        before_charge_max = bytes(memory[1000 + 0x8a8:1000 + 0x8b8])
        before_count_max = bytes(memory[1000 + 0x8c8:1000 + 0x8d8])
        g.start_auto_ultimate()
        self.assertEqual(g.writes, [])
        self.assertTrue(g.pulse_auto_ultimate())
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x8a0), (100, 100))
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x8c0), (1, 1))
        self.assertFalse(g.pulse_auto_ultimate())
        self.assertEqual(len(g.writes), 2)
        self.assertEqual(bytes(memory[1000 + 0x8a8:1000 + 0x8b8]), before_charge_max)
        self.assertEqual(bytes(memory[1000 + 0x8c8:1000 + 0x8d8]), before_count_max)
        g.stop_auto_ultimate()
        struct.pack_into('<ff', memory, 1000 + 0x8a0, 10, 10)
        self.assertFalse(g.pulse_auto_ultimate())
        self.assertEqual(len(g.writes), 2)

    def test_ultimate_lock_stops_on_player_change_or_invalid_capacity(self):
        for invalid_capacity in (False, True):
            g, memory = self.resource_game()
            g.start_auto_ultimate()
            if invalid_capacity:
                struct.pack_into('<ff', memory, 1000 + 0x8b0, 0, 0)
            else:
                g.identity = (1000, 9, 2000, 3000)
            with self.assertRaises(RuntimeError):
                g.pulse_auto_ultimate()
            self.assertEqual(g.writes, [])
            self.assertIsNone(g.auto_ultimate)

    def test_ultimate_lock_second_write_failure_rolls_back_charge_and_stops(self):
        g, memory = self.resource_game()
        g.start_auto_ultimate()
        before, normal = bytes(memory), g.raw_write
        def write(address, payload):
            if address == 1000 + 0x8c0:
                raise DataWriteError('count write failed', 0)
            normal(address, payload)
        g.raw_write = write
        with self.assertRaises(DataWriteError):
            g.pulse_auto_ultimate()
        self.assertEqual(bytes(memory), before)
        self.assertIsNone(g.auto_ultimate)

    def test_close_clears_new_resource_locks_without_writing_counts_back(self):
        g, memory = self.resource_game()
        g.start_auto_medicine('Hulu')
        g.start_auto_ultimate()
        g.handle = None
        before = bytes(memory)
        g.close()
        self.assertEqual(g.auto_medicines, {})
        self.assertIsNone(g.auto_ultimate)
        self.assertEqual(bytes(memory), before)

    @staticmethod
    def health_game(current=700, maximum=850):
        g = Fake()
        memory = bytearray(8192)
        struct.pack_into('<ff', memory, 1000 + 0x60, current, current)
        struct.pack_into('<ff', memory, 1000 + 0x70, maximum, maximum)
        g.context = lambda: {'attributes': 1000}
        g.obj = lambda pointer: {'class': 2000}
        g.property = lambda *args: None
        g.targets = lambda feature: [(1000, 1000 + 0x60, '<ff')]
        g.read = lambda address, size: bytes(memory[address:address + size])
        def write(address, payload):
            g.writes.append(payload)
            memory[address:address + len(payload)] = payload
        g.raw_write = write
        return g, memory

    def test_auto_health_refills_only_current_health_and_stops_when_disabled(self):
        g, memory = self.health_game()
        maximum_before = bytes(memory[1000 + 0x68:1000 + 0x78])
        g.start_auto_health()
        self.assertEqual(g.writes, [])
        self.assertTrue(g.pulse_auto_health())
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x60), (850, 850))
        self.assertEqual(bytes(memory[1000 + 0x68:1000 + 0x78]), maximum_before)
        g.stop_auto_health()
        struct.pack_into('<ff', memory, 1000 + 0x60, 500, 500)
        self.assertFalse(g.pulse_auto_health())
        self.assertEqual(len(g.writes), 1)

    def test_health_floor_changes_only_lower_limit_and_restores_it(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + 0xd80, '<ff')]
        original_health = bytes(memory[1000 + 0x60:1000 + 0x78])
        g.modifier('health_floor', 850)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (850, 850))
        self.assertEqual(bytes(memory[1000 + 0x60:1000 + 0x78]), original_health)
        self.assertEqual(g.restore_modifiers(), {'health_floor': '已恢复'})
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 0))

    def test_health_floor_rejects_value_above_current_maximum(self):
        g, memory = self.health_game()
        with self.assertRaises(ValueError):
            g.modifier('health_floor', 900)
        self.assertEqual(g.writes, [])

    def test_floor_restore_reverts_our_base_but_preserves_game_changed_current(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + 0xd80, '<ff')]
        g.modifier('health_floor', 850)
        struct.pack_into('<f', memory, 1000 + 0xd84, 1)
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 1))
        self.assertNotIn('health_floor', g.undo)

    def test_floor_restore_reverts_our_current_but_preserves_game_changed_base(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + 0xd80, '<ff')]
        g.modifier('health_floor', 850)
        struct.pack_into('<f', memory, 1000 + 0xd80, 2)
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (2, 0))
        self.assertNotIn('health_floor', g.undo)

    def test_floor_mode_fills_health_once_then_checks_without_repeated_health_writes(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        self.assertEqual(len(g.writes), 2)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x60), (850, 850))
        g.check_floor_lock()
        g.check_floor_lock()
        self.assertEqual(len(g.writes), 2)
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 0))
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x60), (850, 850))

    def test_floor_mode_survives_game_current_floor_recalculation_without_healing(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        before_health = bytes(memory[1000 + 0x60:1000 + 0x68])
        struct.pack_into('<f', memory, 1000 + 0xd84, 1)
        g.check_floor_lock()
        self.assertIn('health_floor', g.undo)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (850, 850))
        self.assertEqual(bytes(memory[1000 + 0x60:1000 + 0x68]), before_health)
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 1))

    def test_floor_mode_tracks_changed_maximum_without_filling_health_again(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        struct.pack_into('<ff', memory, 1000 + 0x70, 850, 935)
        g.check_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (935, 935))
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x60), (850, 850))
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 0))

    def test_floor_repair_preserves_latest_game_value_changed_during_intent_log(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        struct.pack_into('<f', memory, 1000 + 0xd84, 1)
        normal = g.log
        def log(row):
            normal(row)
            if row['action'] == 'floor_repair_intent':
                struct.pack_into('<f', memory, 1000 + 0xd84, 2)
        g.log = log
        g.check_floor_lock()
        g.stop_floor_lock()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0xd80), (0, 2))

    def test_floor_repair_refuses_changed_player_without_more_writes(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        g.identity = (1000, 99, 2000, 3000)
        with self.assertRaises(RuntimeError):
            g.check_floor_lock()
        self.assertEqual(len(g.writes), 2)

    def test_floor_repair_stops_on_death_without_reviving(self):
        g, memory = self.health_game()
        g.targets = lambda feature: [(1000, 1000 + (0xd80 if feature == 'health_floor' else 0x60), '<ff')]
        g.start_floor_lock()
        struct.pack_into('<ff', memory, 1000 + 0x60, 0, 0)
        with self.assertRaises(RuntimeError):
            g.check_floor_lock()
        self.assertNotIn('health_floor', g.undo)
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x60), (0, 0))

    def test_auto_health_does_not_refill_after_death(self):
        g, memory = self.health_game()
        g.start_auto_health()
        struct.pack_into('<ff', memory, 1000 + 0x60, 0, 0)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_health()
        self.assertIsNone(g.auto_health)
        self.assertEqual(g.writes, [])

    def test_auto_health_stops_when_player_identity_changes(self):
        g, memory = self.health_game()
        g.start_auto_health()
        g.identity = (1000, 99, 2000, 3000)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_health()
        self.assertIsNone(g.auto_health)
        self.assertEqual(g.writes, [])

    def test_auto_health_stop_clears_state_despite_log_failure(self):
        g, memory = self.health_game()
        g.start_auto_health()
        g.log = lambda row: (_ for _ in ()).throw(OSError('disk full'))
        g.stop_auto_health()
        self.assertIsNone(g.auto_health)




    def test_switch_cooldown_includes_individual_weapon_cooldown(self):
        g = Fake()
        memory = {1100: struct.pack('<dd', 1, 2), 2100: struct.pack('<d', 15)}
        g.context = lambda: {'pawn': 3000}
        g.weapon_cooldown_features = lambda context=None: ['weapon_cooldown:Main:0']
        g.targets = lambda feature: [(1000, 1100, '<dd')] if feature == 'cooldown' else [(2000, 2100, '<d')]
        g.read = lambda address, size: memory[address]
        g.raw_write = lambda address, payload: memory.__setitem__(address, payload)
        g.modifier('cooldown', 0.1)
        self.assertEqual(struct.unpack('<d', memory[2100]), (0.1,))
        self.assertEqual(set(g.undo), {'cooldown', 'weapon_cooldown:Main:0'})
        g.restore_modifiers()
        self.assertEqual(struct.unpack('<dd', memory[1100]), (1, 2))
        self.assertEqual(struct.unpack('<d', memory[2100]), (15,))

    def test_switch_cooldown_second_write_failure_restores_group(self):
        g = Fake()
        original = {1100: struct.pack('<dd', 1, 2), 2100: struct.pack('<d', 15)}
        memory = dict(original)
        g.context = lambda: {'pawn': 3000}
        g.weapon_cooldown_features = lambda context=None: ['weapon_cooldown:Main:0']
        g.targets = lambda feature: [(1000, 1100, '<dd')] if feature == 'cooldown' else [(2000, 2100, '<d')]
        g.read = lambda address, size: memory[address]
        def write(address, payload):
            if address == 2100:
                raise DataWriteError('second write failure', 0)
            memory[address] = payload
        g.raw_write = write
        with self.assertRaises(DataWriteError):
            g.modifier('cooldown', 0.1)
        self.assertEqual(memory, original)
        self.assertEqual(g.undo, {})

    def test_identity_change_between_intent_and_write_prevents_write(self):
        g = Fake()
        g.change_during_log = True
        with self.assertRaises(RuntimeError):
            g.patch('attack', (110, 110))
        self.assertEqual(g.writes, [])

    def test_nonnumeric_target_never_writes(self):
        g = Fake()
        with self.assertRaises(ValueError):
            g.patch('attack', (float('nan'), 110))
        self.assertEqual(g.writes, [])

    def test_restore_does_not_overwrite_new_game_value(self):
        g = Fake()
        record = g.patch('attack', (110, 110))
        g.memory = struct.pack('<ff', 120, 120)
        with self.assertRaises(RuntimeError):
            g.restore_record(record)
        self.assertEqual(len(g.writes), 1)
        self.assertEqual(g.memory, struct.pack('<ff', 120, 120))

    def test_restore_refuses_reused_object_address(self):
        g = Fake()
        record = g.patch('attack', (110, 110))
        g.identity = (1000, 9, 2000, 3000)
        with self.assertRaises(RuntimeError):
            g.restore_record(record)
        self.assertEqual(len(g.writes), 1)

    def test_reapplying_multiplier_does_not_compound(self):
        g = Fake()
        g.modifier('attack', 2)
        g.modifier('attack', 3)
        self.assertEqual(struct.unpack('<ff', g.memory), (300, 300))
        self.assertEqual(g.restore_modifiers(), {'attack': '已恢复'})
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))

    def test_rejected_multiplier_keeps_existing_modifier(self):
        g = Fake()
        g.modifier('attack', 2)
        before = g.memory
        with self.assertRaises(ValueError):
            g.modifier('attack', 21)
        self.assertEqual(g.memory, before)
        self.assertIn('attack', g.undo)

    def test_attack_accepts_twenty_and_rejects_nonfinite_multiplier(self):
        g = Fake()
        g.modifier('attack', 20)
        self.assertEqual(struct.unpack('<ff', g.memory), (2000, 2000))
        for amount in (float('nan'), float('inf'), 0, 0.09, 20.1):
            with self.assertRaises(ValueError):
                g.modifier('attack', amount)
        self.assertEqual(struct.unpack('<ff', g.memory), (2000, 2000))

    def test_attack_tenth_then_twenty_uses_original_baseline(self):
        g = Fake()
        g.modifier('attack', 0.1)
        self.assertEqual(struct.unpack('<ff', g.memory), (10, 10))
        g.modifier('attack', 20)
        self.assertEqual(struct.unpack('<ff', g.memory), (2000, 2000))
        g.restore_modifiers()
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))

    @classmethod
    def incoming_game(cls):
        g, memory = cls.health_game()
        struct.pack_into('<ff', memory, 1000 + 0x140, 1, 1.2)
        struct.pack_into('<ff', memory, 1000 + 0x2d0, 1, 0.8)
        struct.pack_into('<ff', memory, 1000 + 0xd0, 100, 220)
        g.targets = lambda feature: Game.targets(g, feature)
        return g, memory

    def test_incoming_scales_only_player_taken_factors_and_reapply_does_not_compound(self):
        g, memory = self.incoming_game()
        original = bytes(memory)
        attack_before = bytes(memory[1000 + 0xd0:1000 + 0xd8])
        g.modifier('incoming', 0.1)
        self.assertAlmostEqual(struct.unpack_from('<f', memory, 1000 + 0x144)[0], 0.12)
        g.modifier('incoming', 20)
        self.assertAlmostEqual(struct.unpack_from('<f', memory, 1000 + 0x144)[0], 24)
        self.assertAlmostEqual(struct.unpack_from('<f', memory, 1000 + 0x2d4)[0], 16)
        self.assertEqual(bytes(memory[1000 + 0xd0:1000 + 0xd8]), attack_before)
        g.restore_modifiers()
        self.assertEqual(bytes(memory), original)

    def test_incoming_rejects_out_of_range_without_removing_active_values(self):
        g, memory = self.incoming_game()
        g.modifier('incoming', 0.5)
        before = bytes(memory)
        for amount in (0, 0.09, 20.1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                g.modifier('incoming', amount)
        self.assertEqual(bytes(memory), before)
        self.assertEqual(set(g.undo), {'incoming:physical', 'incoming:elemental'})

    def test_incoming_second_write_failure_rolls_back_first_field(self):
        g, memory = self.incoming_game()
        original, normal = bytes(memory), g.raw_write
        def write(address, payload):
            if address == 1000 + 0x2d0:
                raise DataWriteError('elemental write failure', 0)
            normal(address, payload)
        g.raw_write = write
        with self.assertRaises(DataWriteError):
            g.modifier('incoming', 0.5)
        self.assertEqual(bytes(memory), original)
        self.assertEqual(g.undo, {})

    def test_incoming_restore_keeps_game_changed_component_record(self):
        g, memory = self.incoming_game()
        g.modifier('incoming', 0.5)
        struct.pack_into('<ff', memory, 1000 + 0x140, 1, 1.5)
        result = g.restore_modifiers()
        self.assertEqual(struct.unpack_from('<ff', memory, 1000 + 0x140), (1, 1.5))
        self.assertIn('incoming:physical', g.undo)
        self.assertNotIn('incoming:elemental', g.undo)
        self.assertNotEqual(result['incoming:physical'], '已恢复')


    def test_one_hit_replaces_multiplier_without_compounding_and_restores_original(self):
        g = Fake()
        g.modifier('attack', 20)
        g.start_one_hit()
        g.start_one_hit()
        self.assertEqual(struct.unpack('<ff', g.memory), (1000000, 1000000))
        self.assertEqual(g.undo['attack']['attack_mode'], 'one_hit')
        g.stop_one_hit()
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))
        self.assertNotIn('attack', g.undo)

    def test_ordinary_attack_replaces_one_hit_and_toggle_off_keeps_multiplier(self):
        g = Fake()
        g.start_one_hit()
        g.modifier('attack', 20)
        g.stop_one_hit()
        self.assertEqual(struct.unpack('<ff', g.memory), (2000, 2000))
        g.restore_modifiers()
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))

    def test_one_hit_restore_preserves_game_changed_attack(self):
        g = Fake()
        g.start_one_hit()
        g.memory = struct.pack('<ff', 150, 150)
        with self.assertRaises(RuntimeError):
            g.stop_one_hit()
        self.assertEqual(struct.unpack('<ff', g.memory), (150, 150))
        self.assertIn('attack', g.undo)

    def test_partial_write_failure_restores_only_recognizable_own_bytes(self):
        g = Fake()
        normal = g.raw_write
        calls = []

        def fail_once(address, payload):
            calls.append(payload)
            if len(calls) == 1:
                g.memory = payload[:4] + g.memory[4:]
                raise DataWriteError('partial write', 4)
            normal(address, payload)

        g.raw_write = fail_once
        with self.assertRaises(DataWriteError):
            g.patch('attack', (110, 110))
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))

    def test_log_failure_after_write_rolls_back_before_raising(self):
        g = Fake()
        normal = g.log

        def fail_verified(row):
            if row['action'] == 'write_verified':
                raise OSError('disk full')
            normal(row)

        g.log = fail_verified
        with self.assertRaises(OSError):
            g.patch('attack', (110, 110))
        self.assertEqual(struct.unpack('<ff', g.memory), (100, 100))

    def test_expected_owner_token_blocks_new_object_before_any_write(self):
        g = Fake()
        with self.assertRaises(RuntimeError):
            g.patch('shachi', (400, 400), expected_token=(1000, 999, 2000, 3000))
        self.assertEqual(g.writes, [])

    def test_auto_shachi_identity_change_stops_without_refilling(self):
        g = Fake()
        g.auto_shachi = g.identity
        g.context = lambda: {'attributes': 1000}
        g.identity = (1000, 6, 2000, 3000)
        with self.assertRaises(RuntimeError):
            g.pulse_auto_shachi()
        self.assertIsNone(g.auto_shachi)
        self.assertEqual(g.writes, [])

    def test_stop_auto_shachi_is_safe_even_when_log_disk_fails(self):
        g = Fake()
        g.auto_shachi = g.identity
        g.log = lambda row: (_ for _ in ()).throw(OSError('disk full'))
        g.stop_auto_shachi()
        self.assertIsNone(g.auto_shachi)

    def test_second_ultimate_write_failure_rolls_back_first(self):
        g = Fake()
        g.context = lambda: {'attributes': 1000}
        g.obj = lambda pointer: {'class': 2000}
        g.property = lambda *args: None
        g.read = lambda address, size: struct.pack('<f', 100 if address == 1000 + 0x8b4 else 1)
        records = []
        restored = []

        def patch(feature, values, expected_token=None):
            if feature == 'ultimate_count':
                raise RuntimeError('second operation failed')
            record = {'feature': feature}
            records.append(record)
            return record

        g.patch = patch
        g.restore_record = lambda record: restored.append(record)
        with self.assertRaises(RuntimeError):
            g.refill_ultimate()
        self.assertEqual(restored, records)


class RollbackFailureReporting(unittest.TestCase):
    """Step 4: a failed rollback must be reported, keep undo, and never be swallowed."""

    @staticmethod
    def break_after_first(g):
        normal, state = g.raw_write, {'n': 0}
        def write(address, payload):
            state['n'] += 1
            if state['n'] > 1:
                raise DataWriteError('injected write failure', 0)
            normal(address, payload)
        g.raw_write = write

    def assert_rollback_reported(self, g, error, kept_feature):
        self.assertTrue(error.rollback_errors)
        self.assertTrue(error.issue_reported)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn(kept_feature, g.undo)
        issues = g.issues.drain()
        self.assertIn(ROLLBACK, [i['category'] for i in issues])
        self.assertTrue(any(i['feature'] == kept_feature for i in issues))

    def test_patch_rollback_failure_is_reported_not_passed(self):
        g = Fake()
        state = {'n': 0}
        def write(address, payload):
            state['n'] += 1
            if state['n'] == 1:
                g.memory = payload
                raise DataWriteError('readback lost', len(payload))
            raise DataWriteError('rollback write failed', 0)
        g.raw_write = write
        with self.assertRaises(DataWriteError) as caught:
            g.patch('attack', (110, 110))
        self.assertEqual(str(caught.exception), 'readback lost')
        self.assertEqual(caught.exception.rollback_errors, ['rollback write failed'])
        self.assertEqual([i['category'] for i in g.issues.drain()], [ROLLBACK])
        failed = [row for row in g.logs if row['action'] == 'write_failed'][-1]
        self.assertEqual(failed['cleanup'], 'rollback_failed')

    def test_incoming_group_rollback_failure_keeps_undo(self):
        g, memory = Safeguards.incoming_game()
        self.break_after_first(g)
        with self.assertRaises(DataWriteError) as caught:
            g.modifier('incoming', 0.5)
        self.assert_rollback_reported(g, caught.exception, 'incoming:physical')

    def test_movement_group_rollback_failure_keeps_undo(self):
        g, memory = Safeguards.movement_game()
        self.break_after_first(g)
        with self.assertRaises(DataWriteError) as caught:
            g.modifier('movement', 1.5)
        self.assert_rollback_reported(g, caught.exception, 'movement:WalkSpeed')

    def test_cooldown_group_rollback_failure_keeps_undo(self):
        g = Fake()
        memory = {1100: struct.pack('<dd', 1, 2), 2100: struct.pack('<d', 15)}
        g.context = lambda: {'pawn': 3000}
        g.weapon_cooldown_features = lambda context=None: ['weapon_cooldown:Main:0']
        g.targets = lambda feature: [(1000, 1100, '<dd')] if feature == 'cooldown' else [(2000, 2100, '<d')]
        g.read = lambda address, size: memory[address]
        g.raw_write = lambda address, payload: memory.__setitem__(address, payload)
        self.break_after_first(g)
        with self.assertRaises(DataWriteError) as caught:
            g.modifier('cooldown', 0.1)
        self.assert_rollback_reported(g, caught.exception, 'cooldown')

    def test_rollback_failure_then_restore_does_not_double_write(self):
        g, memory = Safeguards.incoming_game()
        normal = g.raw_write
        self.break_after_first(g)
        with self.assertRaises(DataWriteError):
            g.modifier('incoming', 0.5)
        g.raw_write = normal
        original = struct.pack('<ff', 1, 1.2)
        results = g.restore_modifiers()
        self.assertEqual(results, {'incoming:physical': '已恢复'})
        self.assertEqual(bytes(memory[1000 + 0x140:1000 + 0x148]), original)
        self.assertEqual(g.undo, {})
        self.assertEqual(g.restore_modifiers(), {})

    def test_log_oserror_is_counted(self):
        g = Fake()
        g.log = lambda row: (_ for _ in ()).throw(OSError('disk full'))
        g.stop_auto_health()
        g.auto_health = {'token': g.identity}
        g.stop_auto_health()
        self.assertGreaterEqual(g.issues.counts.get(LOG, 0), 1)


if __name__ == '__main__':
    unittest.main()
