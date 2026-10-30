"""Item-specific native dispatch adapters; no game files or inventory edits.

Only the reflected GetItemNumInfo and RemovePropsFromBackPack dispatch pointers
are replaced. Other items/callers and script-VM frames forward to the originals.
Adapters expire after three seconds without a validated UI heartbeat. Executable
pages remain allocated until process exit, so an in-flight call cannot return to
freed memory after disabling. This module does nothing merely by being imported.
"""
import ctypes as C
import ctypes.wintypes as W
import math
import struct

from core import api, open_process, close_handle, image_path, wpm, DataWriteError
from errors import WRITE, report_issue

MAGIC = b'PBZTRV13'
ALLOCATION_SIZE = 8192
CODE_OFFSET = 4096
REMOVE_OFFSET = 5120
QPC_IAT_RVA = 0xa618c98
FUNCTIONS = (
    ('GetItemNumInfo', 0xe01a4e8, 0x65d45d0, bytes.fromhex('48895c24085556574883'),
     (('PropID', 0, 0x2000, 8), ('CurrentNum', 8, 0x80, 4), ('MaxNum', 12, 0x80, 4))),
    ('RemovePropsFromBackPack', 0xe01a808, 0x65d9440, bytes.fromhex('48895c24084889742418'),
     (('Num', 0, 0x80, 4), ('ItemId', 4, 0x2000, 8), ('bSendBroadcast', 12, 0x20000, 1))),
)

alloc_ex = api('VirtualAllocEx', [W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, W.DWORD], C.c_void_p)
protect_ex = api('VirtualProtectEx', [W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, C.POINTER(W.DWORD)], W.BOOL)
flush_cache = api('FlushInstructionCache', [W.HANDLE, C.c_void_p, C.c_size_t], W.BOOL)
qpc = api('QueryPerformanceCounter', [C.POINTER(C.c_int64)], W.BOOL)
qpf = api('QueryPerformanceFrequency', [C.POINTER(C.c_int64)], W.BOOL)


def counter():
    value = C.c_int64()
    if not qpc(C.byref(value)):
        raise RuntimeError('无法读取传送保护计时器')
    return value.value


def frequency():
    value = C.c_int64()
    if not qpf(C.byref(value)) or value.value <= 0:
        raise RuntimeError('传送保护计时器频率无效')
    return value.value


class Code:
    """Small fixed-instruction encoder; relative branches are resolved internally."""
    def __init__(self):
        self.data, self.labels, self.branches = bytearray(), {}, []

    def emit(self, value):
        self.data.extend(bytes.fromhex(value) if isinstance(value, str) else value)

    def imm64(self, value):
        self.emit(struct.pack('<Q', value))

    def label(self, name):
        self.labels[name] = len(self.data)

    def branch(self, opcode, label):
        self.emit(opcode)
        self.branches.append((len(self.data), label))
        self.emit(bytes(4))

    def finish(self):
        for offset, label in self.branches:
            struct.pack_into('<i', self.data, offset, self.labels[label] - offset - 4)
        return bytes(self.data)


def build_adapter(data_address, original, clock_iat, key, kind):
    if kind not in ('query', 'remove'):
        raise ValueError('未知传送适配入口')
    c = Code()
    c.emit('48 b8'); c.imm64(data_address)       # rax = shared guard
    c.emit('83 78 08 01'); c.branch('0f85', 'original')
    c.emit('48 3b 48 10'); c.branch('0f85', 'original')  # current backpack only
    c.emit('44 8b 51 0c 44 3b 50 0c'); c.branch('0f85', 'original')
    c.emit('4c 8b 51 10 4c 3b 50 18'); c.branch('0f85', 'original')
    c.emit('4c 8b 51 20 4c 3b 50 20'); c.branch('0f85', 'original')
    c.emit('48 83 7a 20 00'); c.branch('0f85', 'original')  # FFrame.Code: VM frames forward
    c.emit('4c 8b 52 28 4d 85 d2'); c.branch('0f84', 'original')  # FFrame.Locals
    c.emit('49 bb'); c.imm64(key)
    c.emit('4d 39 1a' if kind == 'query' else '4d 39 5a 04')
    c.branch('0f85', 'original')
    if kind == 'remove':
        c.emit('41 83 3a 01'); c.branch('0f85', 'original')  # only normal one-item consumption
        c.emit('41 80 7a 0c 01'); c.branch('0f85', 'original')
    # Windows x64: preserve arguments, allocate shadow space and aligned counter storage.
    c.emit('51 52 41 50 41 51 48 83 ec 28 48 8d 4c 24 20')
    c.emit('48 b8'); c.imm64(clock_iat)
    c.emit('ff 10 48 8b 44 24 20 48 83 c4 28 41 59 41 58 5a 59')
    c.emit('49 ba'); c.imm64(data_address)
    c.emit('49 3b 42 50'); c.branch('0f87', 'original')  # expired heartbeat -> native behavior
    c.emit('41 83 7a 08 01'); c.branch('0f85', 'original')
    if kind == 'remove':
        c.emit('f0 49 ff 42 30 c3')  # increment skipped-consumption count, return void
    else:
        # Match both reflected out-parameter properties before writing either output.
        c.emit('41 50 45 31 c0 45 31 c9 4c 8b 92 80 00 00 00')
        c.emit('48 b8'); c.imm64(data_address)
        for i in range(4):
            c.emit('4d 85 d2'); c.branch('0f84', 'found')
            c.emit('4d 8b 1a 4c 3b 58 60'); c.branch('0f85', 'max_' + str(i))
            c.emit('4d 8b 42 08')
            c.label('max_' + str(i))
            c.emit('4c 3b 58 68'); c.branch('0f85', 'next_' + str(i))
            c.emit('4d 8b 4a 08')
            c.label('next_' + str(i))
            c.emit('4d 8b 52 10')
        c.label('found')
        c.emit('4d 85 c0'); c.branch('0f84', 'query_fallback')
        c.emit('4d 85 c9'); c.branch('0f84', 'query_fallback')
        c.emit('44 8b 58 58 45 89 18 44 8b 58 5c 45 89 19 f0 48 ff 40 28 41 58 c3')
        c.label('query_fallback'); c.emit('41 58')
    c.label('original')
    c.emit('48 b8'); c.imm64(original)
    c.emit('ff e0')
    result = c.finish()
    if len(result) >= REMOVE_OFFSET - CODE_OFFSET:
        raise RuntimeError('传送适配代码超出保留空间')
    return result


def item_key(game):
    """Resolve the canonical FName even when the backpack has no inventory record."""
    game.refresh_pool()
    matches = set()
    needle = b'Item_Traversing'
    for block, pointer in enumerate(game.blocks):
        data = game.read(pointer, 131072)
        pos = data.find(needle)
        while pos >= 0:
            if pos >= 2:
                entry = pos - 2
                header = struct.unpack_from('<H', data, entry)[0]
                if not entry % 2 and not header & 1 and header >> 6 == len(needle):
                    index = (block << 16) | (entry // 2)
                    if game.name(index) == 'Item_Traversing':
                        matches.add(index)
            pos = data.find(needle, pos + len(needle))
    if len(matches) != 1:
        raise RuntimeError('穿行符名称未就绪或不唯一，未开启传送')
    return matches.pop()


def snapshot(game, key):
    context = game.context()
    owner = game.backpack(context)
    identity = game.token(owner)
    game.property(identity[2], 'Inventories', 0x510, 0x400000000000, 80)
    rows = [(k, p) for k, p in game.map_rows(owner + 0x510) if k == key]
    if len(rows) > 1:
        raise RuntimeError('穿行符库存记录不唯一')
    amount, maximum = 0, 1
    if rows:
        info = game.obj(rows[0][1])
        if info['outer'] != owner or info['flags'] & 0x10 or game.class_name(info['address']) != 'PBZInventoryInfo':
            raise RuntimeError('穿行符库存归属无效')
        for name, offset, kind, size in (('CurrentAmount', 0x380, 0x80, 4),
                                        ('HasMaxAmount', 0x384, 0x80, 4), ('ItemId', 0x388, 0x2000, 8)):
            game.property(info['class'], name, offset, kind, size)
        if game.qword(info['address'] + 0x388) != key:
            raise RuntimeError('穿行符库存编号不匹配')
        amount, maximum = struct.unpack('<ii', game.read(info['address'] + 0x380, 8))
        if not 0 <= amount <= maximum <= 999 or not maximum:
            raise RuntimeError('穿行符数量或上限无效')
    health = struct.unpack('<f', game.read(context['attributes'] + 0x64, 4))[0]
    if not math.isfinite(health) or health <= 0:
        raise RuntimeError('当前角色尚未存活就绪')
    fresh = game.context()
    if fresh != context or game.backpack(fresh) != owner or game.token(owner) != identity:
        raise RuntimeError('当前玩家或背包正在切换')
    return {'token': identity, 'amount': amount, 'maximum': maximum, 'record_present': bool(rows)}


def descriptors(game):
    result = []
    for name, global_rva, native_rva, signature, parameters in FUNCTIONS:
        pointer = game.qword(game.base + global_rva)
        obj = game.obj(pointer)
        if obj['name'] != name or game.class_name(pointer) != 'Function' or game.obj(obj['outer'])['name'] != 'PBZBackPackSubsystem':
            raise RuntimeError('传送函数身份不匹配')
        if struct.unpack('<I', game.read(pointer + 168, 4))[0] != 16 or not struct.unpack('<I', game.read(pointer + 256, 4))[0] & 0x400:
            raise RuntimeError('传送函数签名不匹配')
        fields = {}
        for field, offset, kind, size in parameters:
            game.property(pointer, field, offset, kind, size)
        prop = game.qword(pointer + 80)
        seen = set()
        while prop and prop not in seen and len(seen) < 8:
            seen.add(prop)
            data = game.read(prop, 128)
            fields[game.name(struct.unpack_from('<I', data, 32)[0])] = prop
            prop = struct.unpack_from('<Q', data, 16)[0]
        if set(fields) != {p[0] for p in parameters}:
            raise RuntimeError('传送参数列表变化')
        if name == 'GetItemNumInfo' and any(not game.qword(fields[p] + 56) & 0x100 for p in ('CurrentNum', 'MaxNum')):
            raise RuntimeError('传送输出参数标记变化')
        original = game.base + native_rva
        if game.read(original, len(signature)) != signature:
            raise RuntimeError('传送入口代码变化，可能存在其他模组')
        result.append({'name': name, 'function': pointer, 'token': game.token(pointer),
                       'slot': pointer + 296, 'original': original, 'fields': fields})
    return result


class RemotePages:
    def __init__(self, game):
        self.game, self.address, self.sealed = game, 0, False
        self.handle = open_process(0x438, False, game.pid)
        if not self.handle:
            raise RuntimeError('无法取得传送适配权限')
        try:
            self.check_process()
        except Exception:
            self.close()
            raise

    def check_process(self):
        if not self.game.alive() or image_path(self.handle) != self.game.path:
            raise RuntimeError('传送进程身份已变化')

    def allocate(self):
        self.check_process()
        self.address = alloc_ex(self.handle, None, ALLOCATION_SIZE, 0x3000, 4)
        if not self.address:
            raise RuntimeError('无法分配传送适配页')
        return self.address

    def write(self, address, payload):
        if not self.address <= address < address + len(payload) <= self.address + ALLOCATION_SIZE:
            raise RuntimeError('传送写入超出自有分配页')
        if self.sealed and address + len(payload) > self.address + CODE_OFFSET:
            raise RuntimeError('传送执行页已封存，禁止改写')
        self._write(address, payload)

    def _write(self, address, payload):
        self.check_process()
        done = C.c_size_t()
        buffer = C.create_string_buffer(payload)
        if not wpm(self.handle, address, buffer, len(payload), C.byref(done)) or done.value != len(payload):
            raise DataWriteError('传送写入失败', done.value)
        if self.game.read(address, len(payload)) != payload:
            raise RuntimeError('传送写入读回不一致')

    def seal(self):
        old = W.DWORD()
        if not protect_ex(self.handle, self.address + CODE_OFFSET, 4096, 0x20, C.byref(old)):
            raise RuntimeError('无法封存传送执行页')
        if not flush_cache(self.handle, self.address + CODE_OFFSET, 4096):
            raise RuntimeError('无法刷新传送适配缓存')
        self.sealed = True

    def exchange(self, descriptor, expected, replacement):
        game = self.game
        game.log({'action': 'teleport_dispatch_intent', 'function': descriptor['name'], 'slot': descriptor['slot'],
                  'before': expected, 'after': replacement, 'token': descriptor['token']})
        self.check_process()
        if game.token(descriptor['function']) != descriptor['token'] or game.qword(descriptor['slot']) != expected:
            raise RuntimeError('传送函数已变化，未覆盖其他修改')
        before, after = struct.pack('<Q', expected), struct.pack('<Q', replacement)
        try:
            self._write(descriptor['slot'], after)
        except Exception as error:
            # A short pointer write must not leave a mixed address installed. Only
            # compensate the exact bytes written by this attempt, never a foreign value.
            count = min(8, max(0, getattr(error, 'bytes_written', 8)))
            partial = after[:count] + before[count:]
            try:
                self.check_process()
                if (count and game.token(descriptor['function']) == descriptor['token'] and
                        game.read(descriptor['slot'], 8) in (after, partial)):
                    self._write(descriptor['slot'], before)
            except Exception as rollback:
                raise RuntimeError('传送入口写入失败且恢复失败：' + str(rollback)) from error
            raise
        game.log({'action': 'teleport_dispatch_verified', 'function': descriptor['name'], 'pointer': replacement})

    def close(self):
        # Do not free published executable memory: a game thread may still be inside it.
        if self.handle:
            close_handle(self.handle)
            self.handle = None


class TeleportUnlock:
    def __init__(self, game, pages_factory=RemotePages):
        self.game, self.pages_factory = game, pages_factory
        self.pages = None
        self.entries = []
        self.key = None
        self.requested = False
        self.candidate = None
        self.samples = 0
        self.status = ''
        self.clock_frequency = frequency()

    def prepare(self):
        entries = descriptors(self.game)
        key = item_key(self.game)
        if self.pages:
            if entries != self.entries or key != self.key:
                raise RuntimeError('传送元数据发生变化，请重新连接游戏')
            return
        for entry in entries:
            if self.game.qword(entry['slot']) != entry['original']:
                raise RuntimeError('已有其他传送拦截，未覆盖；请关闭对应面板后重新连接')
        # Verify the exact FFrame locals/out-reference reader for this supported binary.
        if self.game.read(self.game.base + 0x1632fea, 8) != bytes.fromhex('4c8b412849634144'):
            raise RuntimeError('传送调用帧布局无法核对')
        if not 0x10000 <= self.game.qword(self.game.base + QPC_IAT_RVA) < 0x7fffffffffff:
            raise RuntimeError('传送保护计时入口未就绪')
        pages = self.pages_factory(self.game)
        try:
            address = pages.allocate()
            data = bytearray(128)
            data[:8] = MAGIC
            struct.pack_into('<QQ', data, 0x38, entries[0]['original'], entries[1]['original'])
            struct.pack_into('<Q', data, 0x48, key)
            struct.pack_into('<QQ', data, 0x60, entries[0]['fields']['CurrentNum'], entries[0]['fields']['MaxNum'])
            pages.write(address, bytes(data))
            for entry, offset, kind in zip(entries, (CODE_OFFSET, REMOVE_OFFSET), ('query', 'remove')):
                pages.write(address + offset, build_adapter(address, entry['original'], self.game.base + QPC_IAT_RVA, key, kind))
            pages.seal()
        except Exception:
            pages.close()
            raise
        self.pages, self.entries, self.key = pages, entries, key

    def start(self):
        with self.game.lock:
            self.prepare()
            self.disarm()
            try:
                for entry, offset in zip(self.entries, (CODE_OFFSET, REMOVE_OFFSET)):
                    pointer = self.pages.address + offset
                    self.pages.exchange(entry, entry['original'], pointer)
                self.game.log({'action': 'teleport_unlock_started', 'no_inventory_writes': True,
                               'no_game_file_changes': True, 'heartbeat_seconds': 3})
            except Exception:
                self.stop()
                raise
            self.requested = True
            self.candidate, self.samples = None, 0
            self.pulse()

    def disarm(self):
        if self.pages and self.game.alive():
            self.pages.write(self.pages.address + 8, struct.pack('<I', 0))

    def pulse(self):
        if not self.requested:
            return False
        with self.game.lock:
            try:
                for entry, offset in zip(self.entries, (CODE_OFFSET, REMOVE_OFFSET)):
                    if self.game.token(entry['function']) != entry['token'] or self.game.qword(entry['slot']) != self.pages.address + offset:
                        raise RuntimeError('传送分派已被其他程序改变')
                state = snapshot(self.game, self.key)
                identity = state['token']
                self.samples = self.samples + 1 if self.candidate == identity else 1
                self.candidate = identity
                if self.samples < 2:
                    self.disarm()
                    self.status = '随地传送解锁：等待当前角色稳定'
                    return False
                self.disarm()
                owner, index, cls, outer = identity
                self.pages.write(self.pages.address + 12, struct.pack('<IQQQ', index, owner, cls, outer))
                self.pages.write(self.pages.address + 0x58, struct.pack('<ii', max(1, state['amount']), max(1, state['maximum'])))
                self.pages.write(self.pages.address + 0x50, struct.pack('<Q', counter() + 3 * self.clock_frequency))
                self.pages.write(self.pages.address + 8, struct.pack('<I', 1))
                self.status = '随地传送解锁：已启用'
                return True
            except Exception as error:
                self.candidate, self.samples = None, 0
                try:
                    self.disarm()
                except Exception as disarm_error:
                    # The adapter's independent three-second deadline still expires; surface it anyway (step 4).
                    report_issue(self.game, WRITE, 'teleport', '传送心跳失败后解除失败：' + str(disarm_error))
                self.status = '随地传送解锁：暂缓，' + str(error)
                return False

    def stop(self):
        with self.game.lock:
            self.requested = False
            errors = []
            try:
                self.disarm()
            except Exception as error:
                errors.append(str(error))
            if self.pages and self.game.alive():
                for entry, offset in zip(self.entries, (CODE_OFFSET, REMOVE_OFFSET)):
                    try:
                        current = self.game.qword(entry['slot'])
                        if current == entry['original']:
                            continue
                        if current != self.pages.address + offset:
                            raise RuntimeError('传送入口已被其他程序修改，已保留其值')
                        self.pages.exchange(entry, current, entry['original'])
                    except Exception as error:
                        errors.append(str(error))
            self.status = '随地传送解锁：关闭'
            if errors:
                raise RuntimeError('；'.join(errors))
            if self.pages and self.game.alive():
                try:
                    query_hits, skipped = struct.unpack('<QQ', self.game.read(self.pages.address + 0x28, 16))
                    self.game.log({'action': 'teleport_unlock_stopped', 'query_hits': query_hits,
                                   'skipped_consumptions': skipped})
                except Exception:
                    pass  # Diagnostic failure must not prevent a successfully restored close.

    def close(self):
        self.stop()
        if self.pages:
            self.pages.close()
