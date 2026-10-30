"""Verified PBZ data fields; optional teleport dispatch adapter is isolated in its own module."""
from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import struct
import threading
import time

from runtime_paths import RESOURCE_HOME, log_directory
from errors import (LOG, ROLLBACK, IssueQueue, ReadError, RollbackError, VersionError, WriteError,
                    report_issue)

HOME = RESOURCE_HOME
K = C.WinDLL('kernel32', use_last_error=True)


def api(name, args, result):
    f = getattr(K, name)
    f.argtypes, f.restype = args, result
    return f


open_process = api('OpenProcess', [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
close_handle = api('CloseHandle', [W.HANDLE], W.BOOL)
rpm = api('ReadProcessMemory', [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL)
wpm = api('WriteProcessMemory', [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL)
snapshot = api('CreateToolhelp32Snapshot', [W.DWORD, W.DWORD], W.HANDLE)
query_path = api('QueryFullProcessImageNameW', [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL)
exit_code = api('GetExitCodeProcess', [W.HANDLE, C.POINTER(W.DWORD)], W.BOOL)


class ProcessEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('usage', W.DWORD), ('pid', W.DWORD), ('heap', C.c_size_t),
               ('module', W.DWORD), ('threads', W.DWORD), ('parent', W.DWORD), ('priority', W.LONG),
               ('flags', W.DWORD), ('exe', W.WCHAR * 260)]


process_first = api('Process32FirstW', [W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL)
process_next = api('Process32NextW', [W.HANDLE, C.POINTER(ProcessEntry)], W.BOOL)


class DataWriteError(WriteError):
    def __init__(self, message, bytes_written):
        super().__init__(message)
        self.bytes_written = bytes_written


def image_path(handle):
    buffer = C.create_unicode_buffer(32768)
    size = W.DWORD(len(buffer))
    if not query_path(handle, 0, buffer, C.byref(size)):
        raise RuntimeError('无法读取进程路径')
    return Path(buffer.value)


def discover():
    found = []
    snap = snapshot(2, 0)
    if snap == C.c_void_p(-1).value:
        raise RuntimeError('无法枚举游戏进程')
    try:
        entry = ProcessEntry()
        entry.size = C.sizeof(entry)
        ok = process_first(snap, C.byref(entry))
        while ok:
            if entry.exe.casefold() == 'pbz.exe':
                h = open_process(0x1000, False, entry.pid)
                if h:
                    try:
                        p = image_path(h)
                        if p.as_posix().casefold().endswith('/pbz/platform/steam/pbz.exe'):
                            found.append((entry.pid, p))
                    finally:
                        close_handle(h)
            ok = process_next(snap, C.byref(entry))
    finally:
        close_handle(snap)
    if len(found) != 1:
        raise RuntimeError('请先运行游戏；需要且只能存在一个受支持的游戏进程')
    return found[0]


class Game:
    def __init__(self):
        self.lock = threading.RLock()
        self.handle = None
        self.issues = IssueQueue()
        self.undo = {}
        self.auto_shachi = None
        self.auto_spirit_shadow = None
        self.auto_health = None
        self.auto_medicines = {}
        self.auto_ultimate = None
        self.teleport_unlock = None
        self.floor_repair_count = 0
        self.property_cache = {}
        self.names = {}
        self.profile = json.loads((HOME / 'profile.json').read_text(encoding='utf8'))
        self.pid, self.path = discover()
        with self.path.open('rb') as f:
            digest = hashlib.file_digest(f, 'sha256').hexdigest()
        if digest != self.profile['sha256']:
            raise VersionError('游戏版本不匹配，已停止写入；此版本只支持已核对版本')
        self.handle = open_process(0x410, False, self.pid)
        if not self.handle:
            raise RuntimeError('无法读取游戏进程')
        try:
            if image_path(self.handle) != self.path:
                raise RuntimeError('进程身份变化')
            self.base = self.profile['base']
            header = self.read(self.base, 4096)
            nt = struct.unpack_from('<I', header, 60)[0]
            if header[:2] != b'MZ' or header[nt:nt + 4] != b'PE\0\0':
                raise VersionError('游戏映像校验失败')
            if struct.unpack_from('<H', header, nt + 4)[0] != 0x8664:
                raise VersionError('需要已核对的 64 位游戏')
            if struct.unpack_from('<I', header, nt + 80)[0] != self.profile['image_size']:
                raise VersionError('游戏映像大小变化')
            self.refresh_pool()
            if self.name(0) != 'None' or self.name(3) != 'ByteProperty':
                raise VersionError('引擎名称池校验失败')
            self.context()
        except BaseException:
            self.close()
            raise

    def alive(self):
        code = W.DWORD()
        return bool(self.handle and exit_code(self.handle, C.byref(code)) and code.value == 259)

    def read(self, address, size):
        if not self.handle or not (0x10000 <= address < 0x7fffffffffff) or not 0 < size <= 1024 * 1024:
            raise RuntimeError('读取目标无效')
        buffer = C.create_string_buffer(size)
        done = C.c_size_t()
        if not rpm(self.handle, address, buffer, size, C.byref(done)) or done.value != size:
            raise ReadError('游戏内存读取失败，可能正在切场景或已退出')
        return buffer.raw[:size]

    def qword(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def refresh_pool(self):
        self.pool = self.base + self.profile['name_pool_rva']
        block, cursor = struct.unpack('<II', self.read(self.pool + 8, 8))
        if not 0 <= block < 8192 or not 0 < cursor <= 131072:
            raise RuntimeError('名称池布局不匹配')
        self.blocks = struct.unpack('<' + 'Q' * (block + 1), self.read(self.pool + 16, 8 * (block + 1)))

    def name(self, index):
        if index in self.names:
            return self.names[index]
        block, offset = index >> 16, (index & 65535) * 2
        if block >= len(self.blocks):
            self.refresh_pool()
        if block >= len(self.blocks):
            raise RuntimeError('对象名称索引无效')
        header = struct.unpack('<H', self.read(self.blocks[block] + offset, 2))[0]
        length, wide = header >> 6, header & 1
        if not 0 < length <= 1023 or offset + 2 + length * (2 if wide else 1) > 131072:
            raise RuntimeError('对象名称布局无效')
        value = self.read(self.blocks[block] + offset + 2, length * (2 if wide else 1)).decode('utf-16-le' if wide else 'utf8')
        self.names[index] = value
        return value

    def obj(self, address):
        data = self.read(address, 40)
        vtable, flags, index, cls, name, number, outer = struct.unpack('<QIiQIIQ', data)
        if not self.base <= vtable < self.base + self.profile['image_size'] or not 0 <= index < 10000000:
            raise RuntimeError('对象身份校验失败')
        return {'address': address, 'index': index, 'class': cls, 'outer': outer, 'flags': flags, 'name': self.name(name)}

    def token(self, address):
        obj = self.obj(address)
        return (address, obj['index'], obj['class'], obj['outer'])

    def class_name(self, address):
        return self.obj(self.obj(address)['class'])['name']

    def property(self, cls, wanted, expected_offset, expected_type, expected_size):
        key = (cls, wanted)
        if key not in self.property_cache:
            seen = set()
            current = cls
            result = None
            while current and current not in seen and len(seen) < 32:
                seen.add(current)
                field = self.qword(current + 80)
                fields = set()
                while field and field not in fields and len(fields) < 1500:
                    fields.add(field)
                    data = self.read(field, 128)
                    name = self.name(struct.unpack_from('<I', data, 32)[0])
                    if name == wanted:
                        kind = self.qword(struct.unpack_from('<Q', data, 8)[0] + 24)
                        size = struct.unpack_from('<I', data, 52)[0]
                        offset = struct.unpack_from('<I', data, 68)[0]
                        result = (offset, kind, size)
                        break
                    field = struct.unpack_from('<Q', data, 16)[0]
                if result:
                    break
                current = self.qword(current + 64)
            if not result:
                raise RuntimeError('缺少已核对字段：' + wanted)
            self.property_cache[key] = result
        if self.property_cache[key] != (expected_offset, expected_type, expected_size):
            raise RuntimeError('字段布局变化，已停止写入：' + wanted)
        return expected_offset

    def context(self):
        if not self.alive():
            raise RuntimeError('游戏已退出')
        world = self.qword(self.base + self.profile['world_rva'])
        if self.class_name(world) != 'World':
            raise RuntimeError('当前世界尚未就绪')
        gi = self.qword(world + 0x228)
        locals_array, count, capacity = struct.unpack('<Qii', self.read(gi + 0x38, 16))
        if not 0 < count <= capacity <= 16:
            raise RuntimeError('本地玩家尚未就绪')
        local = self.qword(locals_array)
        controller = self.qword(local + 0x30)
        pawn = self.qword(controller + 0x2f0)
        if not pawn or pawn != self.qword(controller + 0x358):
            raise RuntimeError('玩家对象正在切换')
        player = self.obj(pawn)
        if player['flags'] & 0x10:
            raise RuntimeError('不能修改默认对象')
        attributes = self.qword(pawn + 0x758)
        attr = self.obj(attributes)
        if attr['outer'] != pawn or self.class_name(attributes) != 'BasicAttributeSet':
            raise RuntimeError('玩家属性归属校验失败')
        return {'world': world, 'controller': controller, 'pawn': pawn, 'attributes': attributes}

    def map_rows(self, address):
        data = self.read(address, 80)
        array, count, capacity = struct.unpack_from('<Qii', data)
        bits, max_bits = struct.unpack_from('<ii', data, 40)
        if not 0 <= count <= capacity <= 4096 or bits != count or not bits <= max_bits <= 8192:
            raise RuntimeError('映射布局不匹配')
        if not count:
            return []
        secondary = struct.unpack_from('<Q', data, 32)[0]
        flags = self.read(secondary, (bits + 7) // 8) if secondary else data[16:32]
        if len(flags) * 8 < bits:
            raise RuntimeError('映射分配标记无效')
        slots = self.read(array, count * 24)
        rows = []
        for i in range(count):
            if flags[i // 8] & (1 << (i % 8)):
                key, value = struct.unpack_from('<QQ', slots, i * 24)
                rows.append((key, value))
        return rows

    def weapon_cooldown_configs(self, context=None):
        """Resolve equipped main/sub weapon assets, excluding medicines and interactions."""
        context = context or self.context()
        pawn = context['pawn']
        self.property(self.obj(pawn)['class'], 'Character Weapons Component', 0xec0, 0x10000, 8)
        weapons = self.qword(pawn + 0xec0)
        if self.obj(weapons)['outer'] != pawn or self.class_name(weapons) != 'CharacterWeaponsComponent':
            raise RuntimeError('武器组件归属不匹配')
        self.property(self.obj(weapons)['class'], 'RuntimeWeaponData', 0xf8, 0x400000000000, 80)
        raw = self.read(weapons + 0xf8, 80)
        array, count, capacity = struct.unpack_from('<Qii', raw)
        bits, max_bits = struct.unpack_from('<ii', raw, 40)
        if not 0 < count <= capacity <= 32 or bits != count or not bits <= max_bits <= 128:
            raise RuntimeError('武器槽映射布局变化')
        secondary = struct.unpack_from('<Q', raw, 32)[0]
        flags = self.read(secondary, (bits + 7) // 8) if secondary else raw[16:32]
        found = {}
        seen = set()
        for i in range(count):
            if not flags[i // 8] & (1 << (i % 8)):
                continue
            row = array + i * 48
            name, number = struct.unpack('<II', self.read(row, 8))
            slot = self.name(name)
            if number or slot not in ('Weapon.Slot.Main', 'Weapon.Slot.Shachi'):
                continue
            data, length, allocated = struct.unpack('<Qii', self.read(row + 8, 16))
            if not 0 <= length <= allocated <= 8:
                raise RuntimeError('武器槽数组布局变化')
            for index in range(length):
                config = self.qword(data + index * 288)
                if not config or config in seen:
                    continue
                expected = 'PBZ_MainWeaponConfig_C' if slot.endswith('.Main') else 'PBZ_SubWeaponConfig_C'
                if self.class_name(config) != expected or self.obj(config)['flags'] & 0x10:
                    raise RuntimeError('武器配置类型不匹配')
                self.property(self.obj(config)['class'], 'Change Weapon Cooldown', 0xe0, 0x100000000, 8)
                feature = f"weapon_cooldown:{slot.rsplit('.', 1)[1]}:{index}"
                found[feature] = config
                seen.add(config)
        if not found:
            raise RuntimeError('未找到当前玩家的主武器／影之武装配置')
        return found

    def weapon_cooldown_features(self, context=None):
        return list(self.weapon_cooldown_configs(context))

    def backpack(self, context):
        matches = []
        for _, pointer in self.map_rows(context['world'] + 0x9e0):
            try:
                obj = self.obj(pointer)
                if obj['outer'] == context['world'] and self.class_name(pointer) == 'PBZBackPackSubsystem':
                    matches.append(pointer)
            except (RuntimeError, UnicodeError):
                continue
        if len(matches) != 1:
            raise RuntimeError('背包子系统尚未就绪')
        return matches[0]

    def medicine(self, context, kind='Hulu'):
        if kind not in ('Hulu', 'FeiJiu'):
            raise ValueError('仅支持已核对的葫芦和酒')
        backpack = self.backpack(context)
        self.property(self.obj(backpack)['class'], 'MedicineInfos', 0x620, 0x400000000000, 80)
        matches = []
        for key, pointer in self.map_rows(backpack + 0x620):
            if self.name(key & 0xffffffff) != kind or key >> 32:
                continue
            obj = self.obj(pointer)
            if obj['outer'] == backpack and not obj['flags'] & 0x10 and self.class_name(pointer) == 'PBZMedicineInfo':
                matches.append(pointer)
        if len(matches) != 1:
            raise RuntimeError('未找到当前药品：' + ('葫芦' if kind == 'Hulu' else '酒'))
        return backpack, matches[0]

    def medicine_state(self, kind, context=None):
        context = context or self.context()
        backpack, medicine = self.medicine(context, kind)
        for name, offset in [('CurrentTime', 0x50), ('DisplayTime', 0x54), ('MaxTime', 0x58)]:
            self.property(self.obj(medicine)['class'], name, offset, 0x80, 4)
        self.property(self.obj(backpack)['class'], 'MedicineExtraUsage', 0x670, 0x80, 4)
        current, display, maximum = struct.unpack('<iii', self.read(medicine + 0x50, 12))
        extra = struct.unpack('<i', self.read(backpack + 0x670, 4))[0]
        if not 0 < maximum <= 50 or not 0 <= extra <= 50 or not 0 <= current <= maximum + extra or not 0 <= display <= maximum + extra:
            raise RuntimeError('药品次数或上限无效，已停止补充')
        return medicine, current, display, maximum + extra

    def targets(self, feature):
        context = self.context()
        if feature == 'dust':
            pawn = context['pawn']
            self.property(self.obj(pawn)['class'], 'ConsciousnessDust', 0x1040, 0x80, 4)
            return [(pawn, pawn + 0x1040, '<i')]
        if feature.startswith('weapon_cooldown:'):
            config = self.weapon_cooldown_configs(context).get(feature)
            if config is None:
                raise RuntimeError('当前武器已变化，停止写入旧武器配置')
            return [(config, config + 0xe0, '<d')]
        attributes = context['attributes']
        cls = self.obj(attributes)['class']
        if feature.startswith('movement:'):
            name = feature.split(':', 1)[1]
            offsets = {'WalkSpeed': 0xd88, 'JogSpeed': 0xd98, 'SprintSpeed': 0xda8, 'CrouchSpeed': 0xdb8}
            if name not in offsets:
                raise ValueError('未知的移动速度项目')
            offset = offsets[name]
            self.property(cls, name, offset, 0x100000, 16)
            return [(attributes, attributes + offset + 8, '<ff')]
        if feature.startswith('incoming:'):
            known = {'incoming:physical': ('TakenVulnerabilityMultiplier_Physical', 0x138),
                     'incoming:elemental': ('TakenVulnerabilityMultiplier_Elemental', 0x2c8)}
            if feature not in known:
                raise ValueError('未知的承伤类型')
            field, offset = known[feature]
            self.property(cls, field, offset, 0x100000, 16)
            return [(attributes, attributes + offset + 8, '<ff')]
        if feature in ('health', 'health_floor', 'shachi', 'attack', 'ultimate_charge', 'ultimate_count', 'spirit_shadow'):
            field, offset = {'health': ('Health', 0x58), 'shachi': ('Shachi', 0x638), 'attack': ('Attack', 0xc8),
                             'health_floor': ('Health_Min', 0xd78),
                             'spirit_shadow': ('SpiritShadow', 0x8f8),
                             'ultimate_charge': ('UltimateCharge', 0x898), 'ultimate_count': ('UltimateChargeCount', 0x8b8)}[feature]
            self.property(cls, field, offset, 0x100000, 16)
            return [(attributes, attributes + offset + 8, '<ff')]
        if feature == 'medicine' or feature.startswith('medicine:'):
            kind = 'Hulu' if feature == 'medicine' else feature.split(':', 1)[1]
            backpack, medicine = self.medicine(context, kind)
            cls = self.obj(medicine)['class']
            self.property(cls, 'CurrentTime', 0x50, 0x80, 4)
            self.property(cls, 'DisplayTime', 0x54, 0x80, 4)
            return [(medicine, medicine + 0x50, '<ii')]
        if feature == 'cooldown':
            pawn = context['pawn']
            self.property(self.obj(pawn)['class'], 'Character Weapons Component', 0xec0, 0x10000, 8)
            weapons = self.qword(pawn + 0xec0)
            if self.obj(weapons)['outer'] != pawn or self.class_name(weapons) != 'CharacterWeaponsComponent':
                raise RuntimeError('武器组件归属不匹配')
            config = self.qword(weapons + 0xe8)
            cls = self.obj(config)['class']
            self.property(cls, 'Switch Main Weapon Cooldown', 0x9e8, 0x100000000, 8)
            self.property(cls, 'Switch Shachi Weapon Cooldown', 0x9f0, 0x100000000, 8)
            return [(config, config + 0x9e8, '<dd')]
        raise ValueError('不支持的功能')

    def dust_state(self):
        with self.lock:
            owner, address, fmt = self.targets('dust')[0]
            token = self.token(owner)
            amount = struct.unpack(fmt, self.read(address, 4))[0]
            if not 0 <= amount <= 2000000000:
                raise RuntimeError('意识尘埃数量超出已核对范围')
            if self.context()['pawn'] != owner or self.token(owner) != token:
                raise RuntimeError('意识尘埃所属角色已变化')
            return {'amount': amount, 'token': token}

    def set_dust(self, amount, expected_token=None):
        with self.lock:
            # The game's ChangeDustNumInternal Blueprint clamps to 0..2,000,000,000.
            if isinstance(amount, bool) or not isinstance(amount, int) or not 0 <= amount <= 2000000000:
                raise ValueError('意识尘埃数量须为整数 0–2,000,000,000')
            state = self.dust_state()
            if expected_token is not None and tuple(expected_token) != state['token']:
                raise RuntimeError('意识尘埃所属角色已变化，未写入')
            return self.patch('dust', (amount,), expected_token=state['token'])

    def log(self, row):
        folder = log_directory()
        folder.mkdir(parents=True, exist_ok=True)
        row = {'time_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'pid': self.pid, **row}
        with (folder / 'actions.jsonl').open('a', encoding='utf8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
            f.flush()

    def raw_write(self, address, payload):
        if len(payload) not in (4, 8, 16) or not self.alive():
            raise RuntimeError('写入范围或进程状态无效')
        h = open_process(0x438, False, self.pid)
        if not h:
            raise RuntimeError('无法取得数据写入权限')
        try:
            if image_path(h) != self.path:
                raise RuntimeError('写入进程身份变化')
            done = C.c_size_t()
            buffer = C.create_string_buffer(payload)
            ok = wpm(h, address, buffer, len(payload), C.byref(done))
            if not ok or done.value != len(payload):
                raise DataWriteError('数据写入失败，错误码 ' + str(C.get_last_error()), done.value)
        finally:
            close_handle(h)

    def patch(self, feature, values, expected_token=None, expected_player_token=None):
        with self.lock:
            targets = self.targets(feature)
            if len(targets) != 1:
                raise RuntimeError('写入目标数量不匹配')
            owner, address, fmt = targets[0]
            if len(values) != len(struct.unpack(fmt, bytes(struct.calcsize(fmt)))) or not all(math.isfinite(v) for v in values):
                raise ValueError('数值无效')
            token = self.token(owner)
            if expected_token is not None and token != tuple(expected_token):
                raise RuntimeError('写入对象已切换，未写入')
            if expected_player_token is not None and self.token(self.context()['attributes']) != tuple(expected_player_token):
                raise RuntimeError('玩家已切换，未写入')
            before = self.read(address, struct.calcsize(fmt))
            after = struct.pack(fmt, *values)
            record = {'feature': feature, 'owner': owner, 'token': token, 'address': address, 'fmt': fmt,
                      'before': before.hex(), 'after': after.hex()}
            self.log({'action': 'write_intent', **record})
            if self.targets(feature) != targets or self.token(owner) != token:
                raise RuntimeError('玩家或目标已变化，未写入')
            if expected_player_token is not None and self.token(self.context()['attributes']) != tuple(expected_player_token):
                raise RuntimeError('记录写入前玩家已切换，未写入')
            try:
                self.raw_write(address, after)
                observed = self.read(address, len(after))
                if observed != after:
                    raise RuntimeError('游戏立即改写了设置值，请停止并检查')
                self.log({'action': 'write_verified', **record})
            except Exception as exc:
                cleanup = 'target_changed_or_unreadable'
                try:
                    current = self.read(address, len(after))
                    count = getattr(exc, 'bytes_written', 0)
                    partial = after[:count] + before[count:] if 0 < count < len(after) else None
                    if self.token(owner) == token and (current == after or current == partial):
                        self.raw_write(address, before)
                        cleanup = 'restored_after_error' if self.read(address, len(before)) == before else 'restore_readback_mismatch'
                    elif self.token(owner) == token and (feature == 'health_floor' or feature.startswith('movement:')):
                        self.restore_record(record)
                        cleanup = 'attribute_components_restored_after_error'
                except Exception as rollback_error:
                    # Step 4: the rollback itself failed; memory may be half-written. Report, don't swallow.
                    cleanup = 'rollback_failed'
                    exc.rollback_errors = getattr(exc, 'rollback_errors', []) + [str(rollback_error)]
                    exc.issue_reported = True
                    report_issue(self, ROLLBACK, feature, '写入失败后回滚也失败：' + str(rollback_error))
                try:
                    self.log({'action': 'write_failed', 'feature': feature, 'cleanup': cleanup, 'error': str(exc),
                              'rollback_errors': getattr(exc, 'rollback_errors', [])})
                except OSError as log_error:
                    report_issue(self, LOG, feature, log_error)
                raise
            return record

    def restore_record(self, record):
        with self.lock:
            owner, address = record['owner'], record['address']
            if not self.alive() or self.token(owner) != tuple(record['token']):
                raise RuntimeError('原对象已变化，未向旧地址写入')
            if record['feature'] == 'dust' and self.targets('dust') != [(owner, address, '<i')]:
                raise RuntimeError('意识尘埃所属角色已切换，未撤销到旧角色')
            if record['feature'].startswith('movement:') and self.targets(record['feature']) != [(owner, address, record['fmt'])]:
                raise RuntimeError('移动属性所属角色已变化，未向旧角色恢复')
            expected = bytes.fromhex(record['after'])
            original = bytes.fromhex(record['before'])
            current_block = self.read(address, len(expected))
            if current_block != expected:
                movement_partial = record['feature'].startswith('movement:') and any(
                    current_block[offset:offset + 4] == expected[offset:offset + 4]
                    and current_block[offset:offset + 4] != original[offset:offset + 4] for offset in (0, 4))
                if (record['feature'] == 'health_floor' or movement_partial) and record['fmt'] == '<ff' and len(expected) == len(original) == 8:
                    restored, preserved = [], []
                    for offset in (0, 4):
                        wanted = expected[offset:offset + 4]
                        prior = original[offset:offset + 4]
                        current = self.read(address + offset, 4)
                        if current == wanted and current != prior:
                            action = 'restore_movement_component_intent' if movement_partial else 'restore_floor_component_intent'
                            self.log({'action': action, 'component_offset': offset, **record})
                            if self.token(owner) != tuple(record['token']):
                                raise RuntimeError('恢复下限时对象已变化，停止写入')
                            if movement_partial and self.targets(record['feature']) != [(owner, address, record['fmt'])]:
                                raise RuntimeError('恢复移速时玩家已变化，停止写入')
                            if self.read(address + offset, 4) != wanted:
                                preserved.append(offset)
                                continue
                            self.raw_write(address + offset, prior)
                            if self.read(address + offset, 4) != prior:
                                raise RuntimeError('生命下限部分恢复读回不一致')
                            restored.append(offset)
                        elif current != prior:
                            preserved.append(offset)
                    action = 'restore_movement_components_completed' if movement_partial else 'restore_floor_components_completed'
                    self.log({'action': action, 'restored_offsets': restored,
                              'preserved_game_offsets': preserved, **record})
                    return '已撤销移速修改，保留游戏后续变化' if movement_partial else '已撤销下限修改，保留游戏后续变化'
                raise RuntimeError('游戏已改变该数值，保留游戏当前状态，未覆盖')
            self.log({'action': 'restore_intent', **record})
            if record['feature'].startswith('movement:'):
                if self.targets(record['feature']) != [(owner, address, record['fmt'])] or self.token(owner) != tuple(record['token']):
                    raise RuntimeError('恢复移速前角色已变化，未写入')
                if self.read(address, len(expected)) != expected:
                    raise RuntimeError('游戏已改变移动速度，保留游戏当前数值')
            if record['feature'] == 'dust':
                if self.targets('dust') != [(owner, address, '<i')] or self.token(owner) != tuple(record['token']):
                    raise RuntimeError('撤销前角色已切换，未写入')
                if self.read(address, len(expected)) != expected:
                    raise RuntimeError('游戏已改变意识尘埃，保留游戏当前数量')
            self.raw_write(address, original)
            if self.read(address, len(original)) != original:
                raise RuntimeError('恢复读回不一致')
            self.log({'action': 'restore_verified', **record})

    def refill(self, feature, expected_token=None, expected_player_token=None):
        with self.lock:
            context = self.context()
            attr = context['attributes']
            if feature in ('health', 'shachi', 'spirit_shadow'):
                name, offset = {'health': ('Health_Max', 0x68), 'shachi': ('Shachi_Max', 0x658),
                                'spirit_shadow': ('SpiritShadow_Max', 0x908)}[feature]
                self.property(self.obj(attr)['class'], name, offset, 0x100000, 16)
                maximum = struct.unpack('<f', self.read(attr + offset + 12, 4))[0]
                if not 0 < maximum <= 1000000:
                    raise RuntimeError('资源上限无效')
                return self.patch(feature, (maximum, maximum), expected_token=expected_token)
            if feature == 'medicine' or feature.startswith('medicine:'):
                kind = 'Hulu' if feature == 'medicine' else feature.split(':', 1)[1]
                _, _, _, total = self.medicine_state(kind, context)
                return self.patch(feature, (total, total), expected_token=expected_token,
                                  expected_player_token=expected_player_token)
            raise ValueError('未知补充项目')

    def refill_ultimate(self, expected_token=None):
        """Fill the player's two reflected ultimate attributes, preserving capacities."""
        with self.lock:
            context = self.context()
            attr = context['attributes']
            cls = self.obj(attr)['class']
            self.property(cls, 'UltimateCharge_Max', 0x8a8, 0x100000, 16)
            self.property(cls, 'UltimateChargeCount_Max', 0x8c8, 0x100000, 16)
            charge = struct.unpack('<f', self.read(attr + 0x8a8 + 12, 4))[0]
            count = struct.unpack('<f', self.read(attr + 0x8c8 + 12, 4))[0]
            if not 0 < charge <= 1000000 or not 0 < count <= 100:
                raise RuntimeError('精纯杀气上限无效')
            identity = self.token(attr)
            if expected_token is not None and identity != tuple(expected_token):
                raise RuntimeError('精纯杀气所属玩家已变化，未补充')
            records = []
            try:
                records.append(self.patch('ultimate_charge', (charge, charge), expected_token=identity))
                if self.context()['attributes'] != attr or self.token(attr) != identity:
                    raise RuntimeError('补充过程中玩家已切换')
                records.append(self.patch('ultimate_count', (count, count), expected_token=identity))
                self.log({'action': 'ultimate_refill_completed', 'records': records, 'hud_observation_required': True})
            except Exception:
                for record in reversed(records):
                    try:
                        self.restore_record(record)
                    except Exception as cleanup:
                        report_issue(self, ROLLBACK, record['feature'], '精纯杀气补充回滚失败：' + str(cleanup))
                        self.log({'action': 'ultimate_cleanup_failed', 'error': str(cleanup), 'record': record})
                raise
            return records

    def start_auto_medicine(self, kind):
        with self.lock:
            context = self.context()
            medicine, _, _, _ = self.medicine_state(kind, context)
            binding = {'medicine': self.token(medicine), 'player': self.token(context['attributes'])}
            self.log({'action': 'auto_medicine_started', 'kind': kind, 'binding': binding})
            self.auto_medicines[kind] = binding

    def stop_auto_medicine(self, kind=None):
        with self.lock:
            active = self.auto_medicines.pop(kind, None) if kind else dict(self.auto_medicines)
            if kind is None:
                self.auto_medicines.clear()
            if active:
                try:
                    self.log({'action': 'auto_medicine_stopped', 'kind': kind})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)

    def pulse_auto_medicine(self, kind):
        with self.lock:
            binding = self.auto_medicines.get(kind)
            if binding is None:
                return False
            try:
                context = self.context()
                if self.token(context['attributes']) != binding['player']:
                    raise RuntimeError('玩家已切换，药品锁定已停止')
                medicine, current, display, total = self.medicine_state(kind, context)
                if self.token(medicine) != binding['medicine']:
                    raise RuntimeError('药品对象已变化，锁定已停止')
                if current < total or display < total:
                    self.refill('medicine:' + kind, expected_token=binding['medicine'], expected_player_token=binding['player'])
                    return True
                return False
            except Exception:
                self.auto_medicines.pop(kind, None)
                raise

    def start_auto_ultimate(self):
        with self.lock:
            attr = self.context()['attributes']
            self.targets('ultimate_charge')
            self.targets('ultimate_count')
            identity = self.token(attr)
            self.log({'action': 'auto_ultimate_started', 'token': identity})
            self.auto_ultimate = identity

    def stop_auto_ultimate(self):
        with self.lock:
            active = self.auto_ultimate is not None
            self.auto_ultimate = None
            if active:
                try:
                    self.log({'action': 'auto_ultimate_stopped'})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)

    def pulse_auto_ultimate(self):
        with self.lock:
            if self.auto_ultimate is None:
                return False
            try:
                attr = self.context()['attributes']
                if self.token(attr) != self.auto_ultimate:
                    raise RuntimeError('玩家已切换，精纯杀气锁定已停止')
                cls = self.obj(attr)['class']
                values = []
                for name, offset, cap in [('UltimateCharge', 0x898, 1000000), ('UltimateChargeCount', 0x8b8, 100)]:
                    self.property(cls, name, offset, 0x100000, 16)
                    self.property(cls, name + '_Max', offset + 16, 0x100000, 16)
                    current = struct.unpack('<f', self.read(attr + offset + 12, 4))[0]
                    maximum = struct.unpack('<f', self.read(attr + offset + 28, 4))[0]
                    if not math.isfinite(current) or not 0 <= current <= maximum <= cap or maximum <= 0:
                        raise RuntimeError('精纯杀气状态无效，锁定已停止')
                    values.append((current, maximum))
                if any(current < maximum - 0.001 for current, maximum in values):
                    self.refill_ultimate(expected_token=self.auto_ultimate)
                    return True
                return False
            except Exception:
                self.auto_ultimate = None
                raise

    def start_floor_lock(self):
        with self.lock:
            self.stop_auto_health()
            attr = self.context()['attributes']
            identity = self.token(attr)
            self.property(self.obj(attr)['class'], 'Health_Max', 0x68, 0x100000, 16)
            current = struct.unpack('<f', self.read(attr + 0x64, 4))[0]
            maximum = struct.unpack('<f', self.read(attr + 0x74, 4))[0]
            if not math.isfinite(current) or not 0 < current <= maximum <= 1000000:
                raise RuntimeError('当前生命状态无效或已死亡，未开启生命下限锁定')
            record = self.modifier('health_floor', maximum)
            self.floor_repair_count = 0
            try:
                self.refill('health', expected_token=identity)
            except Exception:
                self.stop_floor_lock()
                raise
            return record

    def stop_floor_lock(self):
        with self.lock:
            record = self.undo.get('health_floor')
            if record is not None:
                result = self.restore_record(record)
                del self.undo['health_floor']
                return result

    def check_floor_lock(self):
        """Maintain Health_Min after normal recalculation, without repeatedly writing Health."""
        with self.lock:
            record = self.undo.get('health_floor')
            if record is None:
                raise RuntimeError('生命下限锁定当前未开启')
            try:
                attr = self.context()['attributes']
                if self.token(attr) != tuple(record['token']):
                    raise RuntimeError('玩家已切换，生命下限锁定已停止')
                current = struct.unpack('<f', self.read(attr + 0x64, 4))[0]
                maximum = struct.unpack('<f', self.read(attr + 0x74, 4))[0]
                if not math.isfinite(current) or not 0 < current <= maximum <= 1000000:
                    raise RuntimeError('生命状态无效或已死亡，生命下限保护已停止')
                observed = self.read(record['address'], 8)
                observed_values = struct.unpack('<ff', observed)
                if not all(math.isfinite(v) and 0 <= v <= 1000000 for v in observed_values):
                    raise RuntimeError('游戏生命下限数值无效，停止保护')
                target = struct.pack('<ff', maximum, maximum)
                if observed == bytes.fromhex(record['after']) and observed == target:
                    return False
                old_expected = bytes.fromhex(record['after'])
                original = bytes.fromhex(record['before'])
                # Preserve game-origin changes as the new exit baseline, only for changed components.
                self.log({'action': 'floor_repair_intent', 'observed': observed.hex(),
                          'target': target.hex(), 'record': record})
                updated = self.patch('health_floor', (maximum, maximum), expected_token=record['token'])
                actual_before = bytes.fromhex(updated['before'])
                baseline = b''.join(actual_before[i:i + 4] if actual_before[i:i + 4] != old_expected[i:i + 4]
                                    else original[i:i + 4] for i in (0, 4))
                updated['before'] = baseline.hex()
                self.undo['health_floor'] = updated
                self.floor_repair_count = getattr(self, 'floor_repair_count', 0) + 1
                self.log({'action': 'floor_reapplied', 'count': self.floor_repair_count, 'record': updated,
                          'periodic_health_write': False})
                return True
            except Exception as exc:
                try:
                    self.log({'action': 'floor_lock_stopped', 'reason': str(exc), 'record': record})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)
                try:
                    self.stop_floor_lock()
                except Exception as cleanup:
                    raise RollbackError(str(exc) + '；原下限未能自动恢复：' + str(cleanup), [str(cleanup)]) from exc
                raise

    def start_auto_health(self):
        with self.lock:
            self.stop_floor_lock()
            context = self.context()
            attr = context['attributes']
            self.targets('health')
            self.property(self.obj(attr)['class'], 'Health_Max', 0x68, 0x100000, 16)
            current = struct.unpack('<f', self.read(attr + 0x58 + 12, 4))[0]
            maximum = struct.unpack('<f', self.read(attr + 0x68 + 12, 4))[0]
            if not math.isfinite(current) or not 0 < current <= maximum <= 1000000:
                raise RuntimeError('当前生命状态无效或已死亡，未开启自动回血')
            token = self.token(attr)
            self.log({'action': 'auto_health_started', 'token': token})
            self.auto_health = token

    def stop_auto_health(self):
        with self.lock:
            was_active = self.auto_health is not None
            self.auto_health = None
            if was_active:
                try:
                    self.log({'action': 'auto_health_stopped'})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)

    def pulse_auto_health(self):
        with self.lock:
            if self.auto_health is None:
                return False
            try:
                attr = self.context()['attributes']
                if self.token(attr) != self.auto_health:
                    raise RuntimeError('玩家已切换，自动回血已停止')
                current = struct.unpack('<f', self.read(attr + 0x58 + 12, 4))[0]
                maximum = struct.unpack('<f', self.read(attr + 0x68 + 12, 4))[0]
                if not math.isfinite(current) or not 0 < current <= maximum <= 1000000:
                    raise RuntimeError('生命状态异常或已死亡，自动回血已停止')
                if current < maximum - 0.001:
                    self.refill('health', expected_token=self.auto_health)
                    return True
                return False
            except Exception:
                self.auto_health = None
                raise

    def start_auto_shachi(self):
        with self.lock:
            context = self.context()
            self.targets('shachi')
            token = self.token(context['attributes'])
            self.log({'action': 'auto_shachi_started', 'token': token})
            self.auto_shachi = token

    def stop_auto_shachi(self):
        with self.lock:
            was_active = self.auto_shachi is not None
            self.auto_shachi = None
            if was_active:
                try:
                    self.log({'action': 'auto_shachi_stopped'})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)

    def pulse_auto_shachi(self):
        with self.lock:
            if self.auto_shachi is None:
                return False
            try:
                context = self.context()
                attr = context['attributes']
                if self.token(attr) != self.auto_shachi:
                    raise RuntimeError('玩家已切换，杀气自动补充已停止')
                current = struct.unpack('<f', self.read(attr + 0x638 + 12, 4))[0]
                maximum = struct.unpack('<f', self.read(attr + 0x658 + 12, 4))[0]
                if not math.isfinite(current) or not 0 <= current <= maximum <= 1000000 or maximum <= 0:
                    raise RuntimeError('杀气状态异常，自动补充已停止')
                if current < maximum - 0.001:
                    self.refill('shachi', expected_token=self.auto_shachi)
                    return True
                return False
            except Exception:
                self.auto_shachi = None
                raise

    def start_auto_spirit_shadow(self):
        with self.lock:
            owner, _, _ = self.targets('spirit_shadow')[0]
            token = self.token(owner)
            self.log({'action': 'auto_spirit_shadow_started', 'token': token})
            self.auto_spirit_shadow = token

    def stop_auto_spirit_shadow(self):
        with self.lock:
            active = self.auto_spirit_shadow is not None
            self.auto_spirit_shadow = None
            if active:
                try:
                    self.log({'action': 'auto_spirit_shadow_stopped'})
                except OSError as log_error:
                    report_issue(self, LOG, '', log_error)

    def pulse_auto_spirit_shadow(self):
        with self.lock:
            if self.auto_spirit_shadow is None:
                return False
            try:
                owner, address, _ = self.targets('spirit_shadow')[0]
                if self.token(owner) != self.auto_spirit_shadow:
                    raise RuntimeError('玩家已切换，锁暗源已停止')
                self.property(self.obj(owner)['class'], 'SpiritShadow_Max', 0x908, 0x100000, 16)
                current = struct.unpack('<f', self.read(address + 4, 4))[0]
                maximum = struct.unpack('<f', self.read(owner + 0x914, 4))[0]
                if not math.isfinite(current) or not 0 <= current <= maximum <= 1000000 or maximum <= 0:
                    raise RuntimeError('暗源状态异常，锁暗源已停止')
                if current < maximum - 0.001:
                    self.refill('spirit_shadow', expected_token=self.auto_spirit_shadow)
                    return True
                return False
            except Exception:
                self.auto_spirit_shadow = None
                raise

    def modifier(self, feature, amount):
        with self.lock:
            if feature == 'movement':
                return self.movement_modifier(amount)
            if feature == 'attack':
                if not math.isfinite(amount) or not 0.1 <= amount <= 20:
                    raise ValueError('攻击倍率范围为 0.1–20')
                return self._apply_attack(amount, 'multiplier')
            if feature == 'incoming':
                return self.incoming_modifier(amount)
            if feature == 'cooldown':
                return self.switch_cooldown_modifier(amount)
            if feature != 'health_floor':
                raise ValueError('不支持持续配置修改')
            floor_identity = None
            if feature == 'health_floor':
                attr = self.context()['attributes']
                self.property(self.obj(attr)['class'], 'Health_Max', 0x68, 0x100000, 16)
                maximum = struct.unpack('<f', self.read(attr + 0x68 + 12, 4))[0]
                if not math.isfinite(amount) or not 0 < amount <= maximum <= 1000000:
                    raise ValueError('生命下限必须大于0且不超过当前最大生命')
                floor_identity = self.token(attr)
            if feature in self.undo:
                self.restore_record(self.undo[feature])
                del self.undo[feature]
            owner, address, fmt = self.targets(feature)[0]
            original = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
            if not all(math.isfinite(v) and 0 <= v <= 1000000 for v in original):
                raise RuntimeError('生命下限原值无效')
            values = (amount, amount)
            record = self.patch(feature, values, expected_token=floor_identity)
            self.undo[feature] = record
            return record

    def _apply_attack(self, multiplier, mode):
        """Both attack modes share one undo slot, so they never multiply each other."""
        with self.lock:
            if 'attack' in self.undo:
                self.restore_record(self.undo['attack'])
                del self.undo['attack']
            owner, address, fmt = self.targets('attack')[0]
            identity = self.token(owner)
            original = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
            if not all(math.isfinite(v) and 0 < v <= 1000000 for v in original):
                raise RuntimeError('攻击原值无效')
            record = self.patch('attack', tuple(v * multiplier for v in original), expected_token=identity)
            record['attack_mode'] = mode
            record['multiplier'] = multiplier
            self.undo['attack'] = record
            return record

    def start_one_hit(self):
        return self._apply_attack(10000, 'one_hit')

    def incoming_modifier(self, amount):
        """Scale the local player's physical/elemental taken factors, not shared enemy assets."""
        with self.lock:
            if not math.isfinite(amount) or not 0.1 <= amount <= 20:
                raise ValueError('怪物伤害倍率范围为 0.1–20')
            features = ('incoming:physical', 'incoming:elemental')
            for feature in features:
                if feature in self.undo:
                    self.restore_record(self.undo[feature])
                    del self.undo[feature]
            identity = self.token(self.context()['attributes'])
            plan = []
            for feature in features:
                owner, address, fmt = self.targets(feature)[0]
                if self.token(owner) != identity:
                    raise RuntimeError('承伤属性对象已变化')
                original = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
                if not all(math.isfinite(v) and 0 <= v <= 1000 for v in original):
                    raise RuntimeError('承伤原值无效')
                plan.append((feature, tuple(v * amount for v in original)))
            completed = []
            try:
                for feature, values in plan:
                    record = self.patch(feature, values, expected_token=identity)
                    record['multiplier'] = amount
                    self.undo[feature] = record
                    completed.append(record)
            except Exception as exc:
                self._rollback_batch(completed, exc)
                raise
            return completed

    def restore_movement(self):
        with self.lock:
            results = {}
            for feature, record in list(self.undo.items()):
                if not feature.startswith('movement:'):
                    continue
                try:
                    result = self.restore_record(record)
                    results[feature] = result or '已恢复'
                    del self.undo[feature]
                except Exception as exc:
                    results[feature] = str(exc)
            return results

    def movement_modifier(self, amount):
        with self.lock:
            if not math.isfinite(amount) or not 1 <= amount <= 5:
                raise ValueError('移动加速倍率范围为 1–5')
            self.restore_movement()
            if any(key.startswith('movement:') for key in self.undo):
                raise RuntimeError('原移动速度记录尚未恢复，未叠加新的倍率')
            if amount == 1:
                return []
            identity = self.token(self.context()['attributes'])
            plan = []
            for name in ('WalkSpeed', 'JogSpeed', 'SprintSpeed', 'CrouchSpeed'):
                feature = 'movement:' + name
                owner, address, fmt = self.targets(feature)[0]
                if self.token(owner) != identity:
                    raise RuntimeError('移动属性对象已变化')
                original = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
                if not all(math.isfinite(value) and 0 < value <= 10000 for value in original):
                    raise RuntimeError('移动速度原值无效')
                plan.append((feature, tuple(value * amount for value in original)))
            completed = []
            try:
                for feature, values in plan:
                    record = self.patch(feature, values, expected_token=identity)
                    record['multiplier'] = amount
                    self.undo[feature] = record
                    completed.append(record)
            except Exception as exc:
                self._rollback_batch(completed, exc)
                raise
            return completed

    def stop_one_hit(self):
        with self.lock:
            record = self.undo.get('attack')
            if record and record.get('attack_mode') == 'one_hit':
                result = self.restore_record(record)
                del self.undo['attack']
                return result

    def switch_cooldown_modifier(self, amount):
        """Change both slot intervals and individual weapon cooldowns as one restorable group."""
        with self.lock:
            if not math.isfinite(amount) or not 0.1 <= amount <= 2:
                raise ValueError('切武器间隔范围为 0.1–2 秒')
            previous = [key for key in self.undo if key == 'cooldown' or key.startswith('weapon_cooldown:')]
            for feature in previous:
                self.restore_record(self.undo[feature])
                del self.undo[feature]
            context = self.context()
            identity = self.token(context['pawn'])
            features = ['cooldown'] + self.weapon_cooldown_features(context)
            plan = []
            for feature in features:
                owner, address, fmt = self.targets(feature)[0]
                original = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
                if not all(math.isfinite(v) and 0 <= v <= 120 for v in original):
                    raise RuntimeError('冷却原值无效')
                values = ((amount, amount) if feature == 'cooldown'
                          else tuple(min(v, amount) for v in original))
                if values != original:
                    plan.append((feature, owner, address, fmt, self.token(owner), values))
            completed = []
            try:
                for feature, owner, address, fmt, token, values in plan:
                    if self.token(self.context()['pawn']) != identity:
                        raise RuntimeError('修改过程中玩家已切换')
                    if self.targets(feature) != [(owner, address, fmt)]:
                        raise RuntimeError('修改过程中武器已切换')
                    record = self.patch(feature, values, expected_token=token)
                    self.undo[feature] = record
                    completed.append(record)
            except Exception as exc:
                self._rollback_batch(completed, exc)
                raise
            # Existing bounded trial callers use this primary record; all changes remain in undo.
            return next((r for r in completed if r['feature'] == 'cooldown'), None)

    def _rollback_batch(self, completed, exc):
        """Undo a partially applied group in reverse; failed entries stay in self.undo for manual recovery."""
        failures = []
        for record in reversed(completed):
            try:
                self.restore_record(record)
                del self.undo[record['feature']]
            except Exception as rollback_error:
                failures.append(record['feature'] + '：' + str(rollback_error))
                report_issue(self, ROLLBACK, record['feature'], '批量修改回滚失败，修改仍残留：' + str(rollback_error))
        if failures:
            exc.rollback_errors = getattr(exc, 'rollback_errors', []) + failures
            exc.issue_reported = True
        return failures

    def restore_modifiers(self):
        results = {}
        for feature, record in list(self.undo.items()):
            try:
                result = self.restore_record(record)
                results[feature] = result or '已恢复'
                del self.undo[feature]
            except Exception as exc:
                results[feature] = str(exc)
        return results

    def status(self):
        with self.lock:
            context = self.context()
            attr = context['attributes']
            result = {'pid': self.pid, 'pawn': hex(context['pawn'])}
            for name, offset in (('health', 0x58), ('health_max', 0x68), ('health_min', 0xd78), ('shachi', 0x638), ('shachi_max', 0x658), ('attack', 0xc8)):
                result[name] = struct.unpack('<f', self.read(attr + offset + 12, 4))[0]
            for name, offset in (('ultimate_charge', 0x898), ('ultimate_charge_max', 0x8a8),
                                 ('ultimate_count', 0x8b8), ('ultimate_count_max', 0x8c8)):
                result[name] = struct.unpack('<f', self.read(attr + offset + 12, 4))[0]
            try:
                backpack, medicine = self.medicine(context)
                result['medicine'] = struct.unpack('<i', self.read(medicine + 0x50, 4))[0]
                result['medicine_base_max'] = struct.unpack('<i', self.read(medicine + 0x58, 4))[0]
                result['medicine_extra'] = struct.unpack('<i', self.read(backpack + 0x670, 4))[0]
            except (RuntimeError, UnicodeError):
                result['medicine'] = None
            owner, address, fmt = self.targets('cooldown')[0]
            result['weapon_cooldowns'] = struct.unpack(fmt, self.read(address, struct.calcsize(fmt)))
            result['individual_weapon_cooldowns'] = [
                {'feature': feature, 'name': self.obj(config)['name'],
                 'seconds': struct.unpack('<d', self.read(config + 0xe0, 8))[0]}
                for feature, config in self.weapon_cooldown_configs(context).items()]
            return result

    def start_teleport_unlock(self):
        if self.teleport_unlock is None:
            from teleport_unlock import TeleportUnlock
            self.teleport_unlock = TeleportUnlock(self)
        self.teleport_unlock.start()
        return self.teleport_unlock.status

    def pulse_teleport_unlock(self):
        if self.teleport_unlock:
            self.teleport_unlock.pulse()
            return self.teleport_unlock.status
        return ''

    def stop_teleport_unlock(self):
        if getattr(self, 'teleport_unlock', None):
            self.teleport_unlock.stop()

    def close(self):
        if getattr(self, 'teleport_unlock', None):
            self.teleport_unlock.close()
        self.auto_shachi = None
        self.auto_spirit_shadow = None
        self.auto_health = None
        self.auto_medicines.clear()
        self.auto_ultimate = None
        if self.handle:
            close_handle(self.handle)
            self.handle = None
