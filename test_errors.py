"""Step 4: error categories, issue queue, status-bar summary and session_effects grading (no game, no Tk)."""
import struct
import threading
import unittest

import errors
from errors import (IssueQueue, ReadError, RollbackError, TargetGone, VersionError, WriteError, classify,
                    recent_lines, report_issue, summarize_issues)
from session_effects import SessionEffects


class Categories(unittest.TestCase):
    def test_all_errors_stay_runtime_errors(self):
        for cls in (TargetGone, ReadError, WriteError, RollbackError, VersionError):
            self.assertTrue(issubclass(cls, RuntimeError))

    def test_classify_mapping(self):
        self.assertEqual(classify(TargetGone('x')), errors.TARGET_GONE)
        self.assertEqual(classify(ReadError('x')), errors.READ)
        self.assertEqual(classify(WriteError('x')), errors.WRITE)
        self.assertEqual(classify(VersionError('x')), errors.VERSION)
        self.assertEqual(classify(RollbackError('x', ['y'])), errors.ROLLBACK)
        self.assertEqual(classify(OSError('disk')), errors.LOG)
        self.assertEqual(classify(RuntimeError('plain')), errors.TARGET_GONE)
        self.assertEqual(classify(ValueError('range')), errors.TARGET_GONE)
        plain = WriteError('w')
        plain.rollback_errors = ['r']
        self.assertEqual(classify(plain), errors.ROLLBACK)

    def test_message_text_unchanged(self):
        self.assertEqual(str(RollbackError('原下限未能自动恢复', ['e'])), '原下限未能自动恢复')


class Queue(unittest.TestCase):
    def test_bounded(self):
        q = IssueQueue(maxlen=5)
        for n in range(20):
            q.report(errors.WRITE, 'f', n)
        items = q.drain()
        self.assertEqual([i['message'] for i in items], ['15', '16', '17', '18', '19'])
        self.assertEqual(q.counts[errors.WRITE], 20)
        self.assertEqual(q.drain(), [])

    def test_thread_safe(self):
        q = IssueQueue(maxlen=10000)
        threads = [threading.Thread(target=lambda: [q.report(errors.LOG, '', 'x') for _ in range(500)]) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(q.drain()), 4000)
        self.assertEqual(q.counts[errors.LOG], 4000)

    def test_read_failures_need_threshold_and_reset(self):
        q = IssueQueue()
        for _ in range(errors.READ_THRESHOLD - 1):
            self.assertFalse(q.read_failed('', 'rpm'))
        self.assertEqual(q.drain(), [])
        q.read_ok()
        for _ in range(errors.READ_THRESHOLD - 1):
            q.read_failed('', 'rpm')
        self.assertEqual(q.drain(), [])
        self.assertTrue(q.read_failed('', 'rpm'))
        self.assertEqual([i['category'] for i in q.drain()], [errors.READ])

    def test_report_issue_creates_queue_lazily(self):
        class Bare:
            pass
        game = Bare()
        report_issue(game, errors.ROLLBACK, 'attack', 'boom')
        self.assertEqual(game.issues.drain()[0]['feature'], 'attack')


class Summary(unittest.TestCase):
    def item(self, category):
        return {'category': category, 'feature': 'f', 'message': 'm', 'time': 0}

    def test_colors_by_highest_level(self):
        self.assertEqual(summarize_issues([]), ('', ''))
        self.assertEqual(summarize_issues([self.item(errors.LOG)])[0], 'gray')
        self.assertEqual(summarize_issues([self.item(errors.LOG), self.item(errors.WRITE)])[0], 'yellow')
        self.assertEqual(summarize_issues([self.item(errors.READ), self.item(errors.ROLLBACK)])[0], 'red')
        self.assertEqual(summarize_issues([self.item(errors.VERSION)])[0], 'red')

    def test_text_counts_and_hides_target_gone(self):
        color, text = summarize_issues([self.item(errors.ROLLBACK), self.item(errors.ROLLBACK),
                                        self.item(errors.LOG), self.item(errors.TARGET_GONE)])
        self.assertEqual(text, '回滚失败 2；日志异常 1')
        self.assertEqual(summarize_issues([self.item(errors.TARGET_GONE)]), ('', ''))

    def test_recent_lines_limit(self):
        lines = recent_lines([self.item(errors.WRITE)] * 9)
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], '写入失败·f：m')


class FakeGame:
    """Minimal live player; patch/stop behaviour is injected per test."""

    def __init__(self):
        self.lock = threading.RLock()
        self.undo = {}
        self.logs = []
        self.memory = bytearray(4096)
        struct.pack_into('<ff', self.memory, 0x60, 100, 100)
        struct.pack_into('<ff', self.memory, 0x70, 200, 200)

    def context(self):
        return {'attributes': 0}

    def token(self, owner):
        return (1, 2, 3, 4)

    def read(self, address, size):
        return bytes(self.memory[address:address + size])

    def log(self, row):
        self.logs.append(row)

    def targets(self, feature):
        return [(0, 0xc8, '<ff')]


class SessionGrading(unittest.TestCase):
    def armed_floor(self, failure):
        game = FakeGame()
        game.undo['health_floor'] = {'token': (1, 2, 3, 4)}
        def check():
            raise failure
        game.check_floor_lock = check
        return game, SessionEffects(game)

    def test_target_gone_still_shows_pause(self):
        game, session = self.armed_floor(RuntimeError('角色尚未存活就绪'))
        self.assertFalse(session.floor_step())
        self.assertIn('暂缓', session.floor_status)
        self.assertFalse(session.floor_disabled)
        self.assertIsNone(getattr(game, 'issues', None))

    def test_rollback_failure_is_not_downgraded(self):
        game, session = self.armed_floor(RollbackError('写入失败；原下限未能自动恢复：x', ['x']))
        self.assertFalse(session.floor_step())
        self.assertNotIn('暂缓', session.floor_status)
        self.assertIn('回滚失败', session.floor_status)
        self.assertTrue(session.floor_disabled)
        self.assertEqual([i['category'] for i in game.issues.drain()], [errors.ROLLBACK])
        session.floor_step()  # disabled: no retry, no duplicate report
        self.assertEqual(game.issues.drain(), [])

    def test_attack_write_failure_stops_request(self):
        game = FakeGame()
        struct.pack_into('<ff', game.memory, 0xc8, 10, 10)
        game.undo['attack'] = {'token': (1, 2, 3, 4), 'owner': 0, 'address': 0xc8, 'fmt': '<ff',
                               'after': struct.pack('<ff', 20, 20).hex(), 'before': struct.pack('<ff', 10, 10).hex(),
                               'multiplier': 2}
        def patch(*args, **kwargs):
            raise WriteError('数据写入失败')
        game.patch = patch
        session = SessionEffects(game)
        self.assertFalse(session.attack_step())
        self.assertIsNone(session.attack_request)
        self.assertIn('已停用', session.attack_status)
        self.assertEqual([i['category'] for i in game.issues.drain()], [errors.WRITE])


if __name__ == '__main__':
    unittest.main()
