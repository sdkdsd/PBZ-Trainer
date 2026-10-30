"""Error categories and a thread-safe issue queue shared by core and UI (step 4).

All exception classes subclass RuntimeError so existing ``except RuntimeError``
callers and tests keep working. Core only enqueues issues; the Tk UI drains
them from its existing 1000 ms tick, so no extra thread is introduced.
"""
from __future__ import annotations

from collections import deque
import threading
import time

TARGET_GONE = 'target_gone'
READ = 'read'
WRITE = 'write'
ROLLBACK = 'rollback'
VERSION = 'version'
LOG = 'log'

# Severity → colour. Higher number wins in the status bar.
LEVEL = {TARGET_GONE: 0, LOG: 1, READ: 2, WRITE: 2, VERSION: 3, ROLLBACK: 3}
COLOR = {0: '', 1: 'gray', 2: 'yellow', 3: 'red'}
LABEL = {TARGET_GONE: '目标失效', READ: '读取失败', WRITE: '写入失败', ROLLBACK: '回滚失败',
         VERSION: '版本校验失败', LOG: '日志异常'}
READ_THRESHOLD = 20


class PBZError(RuntimeError):
    category = WRITE


class TargetGone(PBZError):
    category = TARGET_GONE


class ReadError(PBZError):
    category = READ


class WriteError(PBZError):
    category = WRITE


class RollbackError(PBZError):
    category = ROLLBACK

    def __init__(self, message, rollback_errors=()):
        super().__init__(message)
        self.rollback_errors = list(rollback_errors)


class VersionError(PBZError):
    category = VERSION


def classify(exc):
    """Map any exception to a category; rollback failures attached to another error win."""
    if getattr(exc, 'rollback_errors', None):
        return ROLLBACK
    if isinstance(exc, PBZError):
        return exc.category
    if isinstance(exc, OSError):
        return LOG
    # Unclassified errors (object switched, player not ready, invalid input) keep their old
    # message-line behaviour and are treated as transient: no status-bar entry.
    return TARGET_GONE


class IssueQueue:
    """Bounded, thread-safe queue of issues plus counters; never touches Tk."""

    def __init__(self, maxlen=100):
        self._lock = threading.Lock()
        self._items = deque(maxlen=maxlen)
        self.counts = {}
        self.read_failures = 0

    def report(self, category, feature, message):
        with self._lock:
            self._items.append({'time': time.time(), 'category': category,
                                'feature': feature, 'message': str(message)})
            self.counts[category] = self.counts.get(category, 0) + 1

    def drain(self):
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    def read_failed(self, feature, message):
        """Count consecutive read failures; only report once the threshold is reached."""
        with self._lock:
            self.read_failures += 1
            reached = self.read_failures == READ_THRESHOLD
        if reached:
            self.report(READ, feature, '连续 %d 次读取失败：%s' % (READ_THRESHOLD, message))
        return reached

    def read_ok(self):
        with self._lock:
            self.read_failures = 0


def report_issue(game, category, feature, message):
    """Report on ``game.issues`` if present; lazily creates the queue (test fakes skip __init__)."""
    issues = getattr(game, 'issues', None)
    if issues is None:
        try:
            issues = game.issues = IssueQueue()
        except Exception:
            return
    issues.report(category, feature, message)


def summarize_issues(issues):
    """Pure helper for the status bar: ``(color, text)`` from a list of issue dicts."""
    if not issues:
        return '', ''
    level = max(LEVEL.get(i['category'], 2) for i in issues)
    counts = {}
    for item in issues:
        counts[item['category']] = counts.get(item['category'], 0) + 1
    parts = [LABEL.get(c, c) + ' ' + str(n) for c, n in sorted(counts.items(), key=lambda kv: -LEVEL.get(kv[0], 2))
             if LEVEL.get(c, 2) > 0]
    return COLOR[level], '；'.join(parts)


def recent_lines(issues, limit=5):
    return [LABEL.get(i['category'], i['category']) + (('·' + i['feature']) if i['feature'] else '') + '：' + i['message']
            for i in issues[-limit:]]
