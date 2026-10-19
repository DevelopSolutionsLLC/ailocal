"""Metered Brave requests behind one cross-process allowance ledger.

SearXNG's braveapi engine discards Brave's response headers, so this is the
only path that can observe the provider's quota window. Every metered request
is reserved here before it is sent and settled from its outcome afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode

from transport import ResearchError, request

ENDPOINT = 'https://api.search.brave.com/res/v1/web/search'
# Brave documents a monthly window of 2592000 s; this key reported 2678400 s.
LONGEST_WINDOW = 31 * 86400
# A reservation older than this was lost with its process; it stays counted.
ESTABLISHING_SECONDS = 60
SCOPE = 'Brave requests made through ailocal research on this machine'


def _iso(value):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(value)) if value else None


def parse_headers(headers: dict, now: float) -> dict | None:
    """Return the longest quota window, or None when Brave sent no usable headers.

    Brave sends one comma-separated value per window, e.g. Limit `50, 0`,
    Policy `50;w=1, 0;w=2678400`, Remaining `49, 0`, Reset `1, 1120380`.
    A monthly limit of 0 means no monthly cap, so its remaining value says
    nothing about usage.
    """
    try:
        limits = [int(v) for v in headers['x-ratelimit-limit'].split(',')]
        remaining = [int(v) for v in headers['x-ratelimit-remaining'].split(',')]
        resets = [int(v) for v in headers['x-ratelimit-reset'].split(',')]
        policy = headers.get('x-ratelimit-policy')
        windows = ([int(p.split('w=')[1]) for p in policy.split(',')] if policy
                   else [1] + [LONGEST_WINDOW] * (len(limits) - 1))
        if not (len(limits) == len(remaining) == len(resets) == len(windows)) or not limits:
            return None
        i = max(range(len(windows)), key=windows.__getitem__)
        if windows[i] < 86400 or resets[i] < 0 or resets[i] > LONGEST_WINDOW or min(limits[i], remaining[i]) < 0:
            return None
        return {'observed_at': now, 'limit': limits[i], 'remaining': remaining[i],
                'reset_at': now + resets[i], 'window': windows[i]}
    except (KeyError, ValueError, IndexError, AttributeError):
        return None


@dataclass
class Grant:
    id: int | None
    refusal: str | None = None


class Ledger:
    """SQLite with BEGIN IMMEDIATE serializes reservations across sessions."""

    def __init__(self, path: Path, budget: int):
        self.path = path
        self.budget = budget

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.execute('CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, at REAL, state TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS observation (k INTEGER PRIMARY KEY CHECK (k = 1), '
                   'observed_at REAL, lim INTEGER, remaining INTEGER, reset_at REAL, win INTEGER, '
                   'headers_missing_at REAL, quota_limited_until REAL)')
        return db

    @staticmethod
    def _observation(db):
        row = db.execute('SELECT observed_at, lim, remaining, reset_at, win, headers_missing_at, '
                         'quota_limited_until FROM observation WHERE k = 1').fetchone()
        keys = ('observed_at', 'limit', 'remaining', 'reset_at', 'window', 'headers_missing_at', 'quota_limited_until')
        return dict(zip(keys, row)) if row else {}

    @staticmethod
    def _period(obs, now):
        """(start, end, known). Unknown periods count conservatively."""
        if obs.get('reset_at') and obs['reset_at'] > now:
            return obs['reset_at'] - obs['window'], obs['reset_at'], True
        # The previous window ended at its reset; never count from before it,
        # and never from further back than the longest window.
        start = max(obs.get('reset_at') or 0, now - LONGEST_WINDOW)
        return start, None, False

    @staticmethod
    def _counted(db, since):
        return db.execute("SELECT COUNT(*) FROM requests WHERE at >= ? AND state IN ('reserved', 'billed', 'unknown')",
                          (since,)).fetchone()[0]

    def reserve(self, now: float | None = None) -> Grant:
        now = time.time() if now is None else now
        try:
            db = self._connect()
        except (OSError, sqlite3.Error):
            return Grant(None, 'BRAVE_LEDGER_UNAVAILABLE')
        try:
            db.execute('BEGIN IMMEDIATE')
            obs = self._observation(db)
            start, _, known = self._period(obs, now)
            if (obs.get('quota_limited_until') or 0) > now:
                refusal = 'BRAVE_QUOTA_LIMITED'
            elif self._counted(db, start) >= self.budget:
                refusal = 'BRAVE_BUDGET_EXHAUSTED'
            elif known and obs['limit'] > 0 and self._account_left(db, obs) <= 0:
                refusal = 'BRAVE_ACCOUNT_LIMIT'
            elif not known and obs.get('headers_missing_at') is not None and obs['headers_missing_at'] > now - LONGEST_WINDOW:
                # A billed response arrived without quota headers: the period
                # cannot be established, so no further metered request is safe.
                refusal = 'BRAVE_QUOTA_UNKNOWN'
            elif not known and db.execute("SELECT COUNT(*) FROM requests WHERE state = 'reserved' AND at > ?",
                                          (now - ESTABLISHING_SECONDS,)).fetchone()[0]:
                # One request establishes an unknown period; concurrent sessions wait for it.
                refusal = 'BRAVE_QUOTA_ESTABLISHING'
            else:
                refusal = None
            if refusal:
                db.execute('ROLLBACK')
                return Grant(None, refusal)
            grant = db.execute("INSERT INTO requests (at, state) VALUES (?, 'reserved')", (now,)).lastrowid
            db.execute('COMMIT')
            return Grant(grant)
        except sqlite3.Error:
            return Grant(None, 'BRAVE_LEDGER_UNAVAILABLE')
        finally:
            db.close()

    def _account_left(self, db, obs):
        """Account-wide allowance left when Brave reports a monthly cap."""
        used = obs['limit'] - obs['remaining']
        since = db.execute("SELECT COUNT(*) FROM requests WHERE at > ? AND state IN ('reserved', 'billed', 'unknown')",
                           (obs['observed_at'],)).fetchone()[0]
        return min(obs['remaining'], self.budget - used) - since

    def settle(self, grant: Grant, status: int | None, headers: dict | None = None,
               error_code: str | None = None, now: float | None = None) -> None:
        """Brave bills only non-error responses; a lost response stays counted."""
        now = time.time() if now is None else now
        if status is None:
            state = 'unknown'
        else:
            state = 'billed' if 200 <= status < 300 else 'not_billed'
        try:
            db = self._connect()
        except (OSError, sqlite3.Error):
            return
        try:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE requests SET state = ? WHERE id = ?', (state, grant.id))
            db.execute('INSERT OR IGNORE INTO observation (k) VALUES (1)')
            observed = parse_headers(headers or {}, now)
            if observed:
                db.execute('UPDATE observation SET observed_at = ?, lim = ?, remaining = ?, reset_at = ?, win = ?, '
                           'headers_missing_at = NULL WHERE k = 1',
                           (observed['observed_at'], observed['limit'], observed['remaining'],
                            observed['reset_at'], observed['window']))
            elif state == 'billed':
                db.execute('UPDATE observation SET headers_missing_at = ? WHERE k = 1', (now,))
            if status in (402, 429) and error_code != 'RATE_LIMITED':
                until = observed['reset_at'] if observed else now + 3600
                db.execute('UPDATE observation SET quota_limited_until = ? WHERE k = 1', (until,))
            db.execute('COMMIT')
        except sqlite3.Error:
            pass
        finally:
            db.close()

    def status(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        try:
            db = self._connect()
            try:
                obs = self._observation(db)
                start, end, known = self._period(obs, now)
                counted = self._counted(db, start)
                account = (self._account_left(db, obs) if known and obs['limit'] > 0 else None)
                refused_until = obs.get('quota_limited_until') or 0
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            return {'budget': self.budget, 'counted_this_period': None, 'budget_remaining': None,
                    'scope': SCOPE, 'state': 'ledger unavailable; Brave is not used'}
        report = {'budget': self.budget, 'counted_this_period': counted,
                  'budget_remaining': max(0, self.budget - counted), 'scope': SCOPE,
                  'period_resets_at': _iso(end), 'quota_observed_at': _iso(obs.get('observed_at'))}
        if not known:
            report['period_basis'] = ('unknown: no current Brave quota observation; counted conservatively '
                                      'over the longest possible window')
        if refused_until > now:
            # Brave's own refusal is the one account-wide fact a prepaid key
            # exposes: it covers every consumer of the key, not just this ledger.
            report['account_remaining'] = 0
            report['account_basis'] = (f'Brave refused a request for quota, usage limit or prepaid balance; '
                                       f'no metered request until {_iso(refused_until)}')
        elif account is not None:
            report['account_remaining'] = max(0, account)
            report['account_basis'] = 'Brave X-RateLimit headers at quota_observed_at minus requests counted since'
        else:
            report['account_remaining'] = 'unknown'
            # Prepaid keys report a monthly limit of 0: spending is bounded by the
            # dollar balance and usage limit, which only the dashboard shows.
            report['account_basis'] = ('Brave reports no request quota for this key (prepaid plans are billed '
                                       'from a credit balance) and publishes no balance API; the Brave dashboard '
                                       'shows the balance, and other consumers of the key are not visible here'
                                       if known else 'no current Brave quota observation')
        return report


def summary(report: dict, used: int, refusal: str | None) -> str:
    """One line for the research summary; never claims account-wide remaining it cannot know."""
    if report.get('counted_this_period') is None:
        return 'Brave: not used; the allowance ledger is unavailable.'
    head = f'Brave: {used} used this call' if used else 'Brave: not used this call'
    if refusal:
        head += f' ({refusal})'
    line = (f'{head}; {report["budget_remaining"]} of {report["budget"]} left by ailocal count '
            f'(this machine only')
    if report['account_remaining'] == 'unknown':
        line += '; account-wide usage unknown'
    elif report['account_basis'].startswith('Brave refused'):
        line += '; account-wide exhausted per Brave'
    else:
        line += f'; account-wide {report["account_remaining"]} as of {report["quota_observed_at"]}'
    line += ')'
    line += f'; resets {report["period_resets_at"]}.' if report['period_resets_at'] else '; reset time unknown.'
    return line


class Brave:
    def __init__(self, key: str, ledger: Ledger, endpoint: str = ENDPOINT, session_cap: int = 30):
        self.key = key
        self.ledger = ledger
        self.endpoint = endpoint
        self.session_cap = session_cap
        self.session_used = 0

    @property
    def configured(self):
        return bool(self.key)

    async def search(self, query: str, result_filter: str = 'web') -> list[dict]:
        """One metered request, reserved first. Raises ResearchError with a refusal code."""
        if not self.key:
            raise ResearchError('BRAVE_NOT_CONFIGURED')
        if self.session_used >= self.session_cap:
            raise ResearchError('BRAVE_SESSION_CAP')
        grant = self.ledger.reserve()
        if grant.refusal:
            raise ResearchError(grant.refusal)
        self.session_used += 1
        args = urlencode({'q': query, 'count': 10, 'result_filter': result_filter, 'text_decorations': 0})
        try:
            status, headers, body = await request(self.endpoint + '?' + args, timeout=15,
                                                  headers={'X-Subscription-Token': self.key,
                                                           'Accept': 'application/json'})
        except ResearchError as error:
            # Brave may have billed a request whose response was lost.
            self.ledger.settle(grant, None)
            raise ResearchError('BRAVE_' + str(error).split(':')[0]) from None
        code = None
        try:
            data = json.loads(body)
            if status != 200:
                code = str((data.get('error') or {}).get('code', ''))[:40] or None
        except (ValueError, AttributeError):
            data = None
        self.ledger.settle(grant, status, headers, code)
        if status != 200:
            known = {'RATE_LIMITED', 'QUOTA_LIMITED', 'SUBSCRIPTION_TOKEN_INVALID', 'OPTION_NOT_IN_PLAN'}
            raise ResearchError(f'BRAVE_HTTP_{status}' + (f'_{code}' if code in known else ''))
        if not isinstance(data, dict):
            raise ResearchError('BRAVE_INVALID_JSON')
        results = []
        for kind in ('discussions', 'web'):
            for item in ((data.get(kind) or {}).get('results') or []):
                if isinstance(item, dict):
                    forum = (item.get('data') or {}).get('forum_name') if kind == 'discussions' else None
                    results.append({'url': item.get('url', ''), 'title': item.get('title', ''),
                                    'content': item.get('description', ''), 'kind': kind, 'forum': forum})
        return results
