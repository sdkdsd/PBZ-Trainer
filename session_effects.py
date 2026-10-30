"""Retain user intent across transient reads and validated player replacement."""
import math
import struct

from errors import ROLLBACK, WRITE, classify, report_issue


class SessionEffects:
    def __init__(self, game):
        self.game = game
        self.attack_request = None
        self.retired = {}
        self.stable = {}
        self.floor_status = ''
        self.attack_status = ''
        self.floor_disabled = False  # set when a write/rollback failure stopped the floor lock
        self.capture_attack()

    def capture_attack(self):
        record = self.game.undo.get('attack')
        self.attack_request = ({'multiplier': record['multiplier'], 'mode': record.get('attack_mode', 'multiplier')}
                               if record and 'multiplier' in record else None)

    def clear_attack(self):
        self.attack_request = None
        self.attack_status = ''

    def _attributes(self):
        context = self.game.context()
        owner = context['attributes']
        identity = self.game.token(owner)
        current = struct.unpack('<f', self.game.read(owner + 0x64, 4))[0]
        maximum = struct.unpack('<f', self.game.read(owner + 0x74, 4))[0]
        if not math.isfinite(current) or not math.isfinite(maximum) or not 0 < current <= maximum <= 1000000:
            raise RuntimeError('角色尚未存活就绪')
        return owner, identity

    def _settled(self, feature, identity):
        old, count = self.stable.get(feature, (None, 0))
        count = count + 1 if old == identity else 1
        self.stable[feature] = (identity, count)
        return count >= 2

    def _retire(self, feature, identity):
        record = self.game.undo.get(feature)
        if record is not None and tuple(record['token']) != identity:
            self.game.log({'action': 'effect_owner_retired', 'feature': feature,
                           'record': record, 'new_token': identity, 'writes_to_old_owner': False})
            self.retired[(feature, tuple(record['token']))] = record
            del self.game.undo[feature]
        saved = self.retired.get((feature, identity))
        if saved is not None:
            targets = self.game.targets(feature)
            if targets != [(saved['owner'], saved['address'], saved['fmt'])] or self.game.token(saved['owner']) != identity:
                raise RuntimeError('恢复记录的目标尚未稳定')
            self.game.undo[feature] = saved
            del self.retired[(feature, identity)]
        while len(self.retired) > 64:
            self.retired.pop(next(iter(self.retired)))

    def _serious(self, feature, error):
        """Step 4: write/rollback failures must not be shown as a harmless "暂缓"."""
        category = classify(error)
        if category not in (ROLLBACK, WRITE):
            return None
        if not getattr(error, 'issue_reported', False):
            report_issue(self.game, category, feature, error)
            try:
                error.issue_reported = True
            except Exception:
                pass
        return category

    def floor_step(self):
        if self.floor_disabled:
            return False
        with self.game.lock:
            try:
                owner, identity = self._attributes()
                record = self.game.undo.get('health_floor')
                if record is None or tuple(record['token']) != identity:
                    if not self._settled('health_floor', identity):
                        self.floor_status = '强锁血：等待角色稳定，已保留开启设置'
                        return False
                    self._retire('health_floor', identity)
                    if 'health_floor' not in self.game.undo:
                        self.game.start_floor_lock()
                        self.game.log({'action': 'floor_resumed', 'token': identity})
                repaired = self.game.check_floor_lock()
                self.floor_status = '强锁血：保持开启'
                return repaired
            except Exception as error:
                self.stable.pop('health_floor', None)
                category = self._serious('health_floor', error)
                if category is not None:
                    self.floor_disabled = True
                    self.floor_status = ('强锁血：已停用，' + ('回滚失败，可能有残留修改' if category == ROLLBACK
                                                         else '写入失败') + '（' + str(error) + '）')
                    return False
                self.floor_status = '强锁血：暂缓写入，等待角色就绪（' + str(error) + '）'
                return False

    def attack_step(self):
        if self.attack_request is None:
            return False
        with self.game.lock:
            try:
                owner, identity = self._attributes()
                record = self.game.undo.get('attack')
                if record is None or tuple(record['token']) != identity:
                    if not self._settled('attack', identity):
                        self.attack_status = '攻击倍率：等待角色稳定，设置已保留'
                        return False
                    self._retire('attack', identity)
                    if 'attack' not in self.game.undo:
                        request = self.attack_request
                        self.game._apply_attack(request['multiplier'], request['mode'])
                        self.game.log({'action': 'attack_resumed', 'token': identity, **request})
                    record = self.game.undo['attack']
                targets = self.game.targets('attack')
                if targets != [(record['owner'], record['address'], record['fmt'])] or self.game.token(owner) != tuple(record['token']):
                    raise RuntimeError('攻击属性正在切换')
                observed = self.game.read(record['address'], 8)
                values = struct.unpack('<ff', observed)
                if not all(math.isfinite(v) and 0 < v <= 10000000 for v in values):
                    raise RuntimeError('攻击属性尚未有效')
                expected = bytes.fromhex(record['after'])
                if observed != expected:
                    # Reuse the fixed applied target; never multiply already-scaled observations.
                    updated = self.game.patch('attack', struct.unpack('<ff', expected), expected_token=identity)
                    actual = bytes.fromhex(updated['before'])
                    original = bytes.fromhex(record['before'])
                    updated['before'] = b''.join(actual[i:i+4] if actual[i:i+4] != expected[i:i+4]
                                                else original[i:i+4] for i in (0,4)).hex()
                    updated.update(multiplier=record['multiplier'], attack_mode=record.get('attack_mode', 'multiplier'))
                    self.game.undo['attack'] = updated
                    self.game.log({'action': 'attack_reapplied', 'record': updated, 'compounded': False})
                self.attack_status = '攻击倍率：已维持'
                return observed != expected
            except Exception as error:
                self.stable.pop('attack', None)
                category = self._serious('attack', error)
                if category is not None:
                    self.attack_request = None
                    self.attack_status = ('攻击倍率：已停用，' + ('回滚失败，可能有残留修改' if category == ROLLBACK
                                                          else '写入失败') + '（' + str(error) + '）')
                    return False
                self.attack_status = '攻击倍率：暂缓写入，等待角色就绪（' + str(error) + '）'
                return False
