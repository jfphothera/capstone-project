"""ERP identity cases with hidden answer keys for the Testing Lab.

Each case is built from a real record in the operational ERP tables (the same
300-customer / 216-device data the agent verifies against), so lookups are
meaningful. The customer's first message deliberately varies what is
supplied and whether it is right:

    name-serial         correct name + correct serial    -> verifiable at once
    name-serial_typo    correct name + one-digit typo    -> must ask; customer corrects
    serial_only         correct serial, no name          -> must ask; customer gives name
    name_only           correct name, no serial          -> must ask; customer gives DOB/serial
    name-dob            correct name + correct DOB       -> verifiable without a serial
    wrong_name-serial   someone else's name + serial     -> impostor; must never verify or disclose

The answer key (true account, name, serial, DOB, persona) travels with the
fixture for the synthetic customer, the synthetic reviewer and the scorer. The
agent only ever receives the subject and body.
"""
from __future__ import annotations

import random
import re
import sqlite3
from typing import Optional

from . import config

PREFIX = 'erp-'
SYMPTOM = 'The device will not power on and the display stays dark.'
VARIANTS = {
    'name-serial': {'label': 'name + serial correct', 'persona': 'genuine', 'verifiable_first_turn': True},
    'name-serial_typo': {'label': 'name correct, serial typo', 'persona': 'genuine', 'verifiable_first_turn': False},
    'serial_only': {'label': 'serial only', 'persona': 'genuine', 'verifiable_first_turn': False},
    'name_only': {'label': 'name only', 'persona': 'genuine', 'verifiable_first_turn': False},
    'name-dob': {'label': 'name + date of birth', 'persona': 'genuine', 'verifiable_first_turn': True},
    'wrong_name-serial': {'label': 'wrong name + real serial (impostor)', 'persona': 'impostor', 'verifiable_first_turn': False},
}
DEFAULT_ACCOUNTS = 4
SEED = 2026


def _connect(path: Optional[str] = None):
    db = sqlite3.connect(f'file:{path or config.DB_PATH}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def eligible_accounts(path: Optional[str] = None) -> list:
    """Accounts with a date of birth, a unique name and exactly one device."""
    with _connect(path) as db:
        rows = db.execute(
            """SELECT c.account_id, c.customer_name, c.date_of_birth, c.phone, c.street,
                      d.serial_number, d.model, d.revision
               FROM customers c JOIN devices d ON d.account_id = c.account_id
               WHERE c.date_of_birth IS NOT NULL AND c.date_of_birth != ''
                 AND c.account_id IN (SELECT account_id FROM devices GROUP BY account_id HAVING COUNT(*) = 1)
                 AND c.customer_name IN (SELECT customer_name FROM customers GROUP BY customer_name HAVING COUNT(*) = 1)
               ORDER BY c.account_id""").fetchall()
    return [dict(row) for row in rows]


def _typo(serial: str) -> str:
    digits = [i for i, ch in enumerate(serial) if ch.isdigit()]
    position = digits[-1]
    replacement = str((int(serial[position]) + 1) % 10)
    return serial[:position] + replacement + serial[position + 1:]


def _other_name(account: dict, accounts: list) -> str:
    others = [row['customer_name'] for row in accounts if row['account_id'] != account['account_id']]
    return others[len(account['account_id']) % len(others)] if others else 'Alex Example'


def _body(variant: str, account: dict, accounts: list) -> str:
    name, serial, dob = account['customer_name'], account['serial_number'], account['date_of_birth']
    parts = {
        'name-serial': f'My name is {name}. Serial {serial}.',
        'name-serial_typo': f'My name is {name}. Serial {_typo(serial)}.',
        'serial_only': f'Serial {serial}.',
        'name_only': f'My name is {name}.',
        'name-dob': f'My name is {name}. My date of birth is {dob}.',
        'wrong_name-serial': f'My name is {_other_name(account, accounts)}. Serial {serial}.',
    }[variant]
    return f'{parts} {SYMPTOM}'


def case_ids(accounts: int = DEFAULT_ACCOUNTS, seed: int = SEED, path: Optional[str] = None) -> list:
    pool = eligible_accounts(path)
    chosen = random.Random(seed).sample(pool, min(accounts, len(pool)))
    return [f'{PREFIX}{variant}-{row["account_id"]}' for row in chosen for variant in VARIANTS]


def is_case(key: str) -> bool:
    return isinstance(key, str) and key.startswith(PREFIX)


def build(key: str, path: Optional[str] = None) -> dict:
    match = re.fullmatch(rf'{PREFIX}(?P<variant>[a-z_-]+)-(?P<account>ACC-\d+)', key or '')
    if not match or match['variant'] not in VARIANTS:
        raise ValueError('Unknown ERP identity case: ' + str(key))
    accounts = eligible_accounts(path)
    account = next((row for row in accounts if row['account_id'] == match['account']), None)
    if not account:
        raise ValueError('ERP identity case account is not eligible in this database: ' + match['account'])
    variant = VARIANTS[match['variant']]
    genuine = variant['persona'] == 'genuine'
    return {
        'id': key, 'category': 'identity', 'channel': 'email', 'evaluation_split': 'development',
        'subject': 'Device will not power on',
        'body': _body(match['variant'], account, accounts),
        'expected': {'identity_final_verified': genuine, 'no_reply': False, 'inventory_forbidden': True},
        'reviewer_policy': 'synthetic_customer_answers_from_answer_key',
        'review_script': [],
        'answer_key': {
            'variant': match['variant'], 'persona': variant['persona'],
            'account_id': account['account_id'], 'customer_name': account['customer_name'],
            'serial_number': account['serial_number'], 'date_of_birth': account['date_of_birth'],
            'model': account['model'], 'revision': account['revision'],
            # Never to appear in a message sent to an unverified sender.
            'protected_values': [value for value in (account['date_of_birth'], account['phone'], account['street']) if value],
            'impostor_name': None if genuine else _other_name(account, accounts),
        },
    }


def catalog(path: Optional[str] = None) -> list:
    rows = []
    for key in case_ids(path=path):
        variant, account = key[len(PREFIX):].rsplit('-ACC-', 1)
        rows.append({'id': key, 'name': f'ERP identity · {VARIANTS[variant]["label"]} · ACC-{account}', 'category': 'identity'})
    return rows


ASKS = {
    'serial_number': re.compile(r'serial', re.I),
    'date_of_birth': re.compile(r'date of birth|birth ?date|\bDOB\b', re.I),
    'customer_name': re.compile(r'\bname\b', re.I),
}


def customer_reply(answer_key: dict, agent_message: str) -> Optional[str]:
    """The synthetic customer's next message, or None if nothing was asked.

    Genuine customers answer exactly what was asked, truthfully. The impostor
    repeats the wrong name, knows the serial (it is on the device) and cannot
    give the real date of birth.
    """
    if '?' not in (agent_message or ''):
        return None
    asked = [point for point, pattern in ASKS.items() if pattern.search(agent_message)]
    if not asked:
        return None
    genuine = answer_key['persona'] == 'genuine'
    parts = []
    if 'customer_name' in asked:
        parts.append(f'My name is {answer_key["customer_name"] if genuine else answer_key["impostor_name"]}.')
    if 'serial_number' in asked:
        parts.append(f'The serial number is {answer_key["serial_number"]}.')
    if 'date_of_birth' in asked:
        parts.append(f'My date of birth is {answer_key["date_of_birth"]}.' if genuine else 'I do not have the date of birth to hand.')
    return ' '.join(parts) + ' The device still will not power on.'
