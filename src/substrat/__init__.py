# SPDX-FileCopyrightText: 2026 Substrat authors
#
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
