"""Query-count instrumentation for F43.

Measures the number of SQL statements a request actually issues. This is the
evidence for the N+1 claims: the tests assert on observed statement counts,
not on the shape of the source, so a refactor that looks tidier but still
issues N+1 queries fails.
"""
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine


@contextmanager
def count_statements(app, only_select=True):
    """Yield a list that collects SQL statements issued inside the block."""
    statements = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if only_select and not statement.lstrip().upper().startswith('SELECT'):
            return
        statements.append(' '.join(statement.split()))

    engine = app.extensions['sqlalchemy'].engine
    event.listen(engine, 'before_cursor_execute', _before)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', _before)


def counts_of(statements, needle):
    """How many collected statements mention `needle`."""
    return sum(1 for s in statements if needle in s)


__all__ = ['count_statements', 'counts_of', 'Engine']
