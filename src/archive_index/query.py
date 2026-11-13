"""Read-only FTS5 ranking with exact paragraph and source citations."""
from pathlib import Path
from contextlib import closing
import sqlite3


def search(database, expression, collection=None, limit=10):
    if not isinstance(expression,str) or not expression.strip() or len(expression) > 4096:
        raise ValueError('query must contain 1..4096 characters')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be an integer in 1..100')
    path = Path(database).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('unsupported search database version')
        rows = connection.execute('''
            SELECT d.id,d.title,d.collection,d.source_path,d.sha256,
                   c.paragraph,c.start_line,c.end_line,c.body,
                   bm25(search,2.0,1.0) AS rank
            FROM search JOIN chunks c ON c.id=search.rowid
            JOIN documents d ON d.id=c.document_id
            WHERE search MATCH ? AND (? IS NULL OR d.collection=?)
            ORDER BY rank,d.id,c.paragraph LIMIT ?
            ''', (expression,collection,collection,limit)).fetchall()
    keys = ['document_id','title','collection','source_path','source_sha256',
            'paragraph','start_line','end_line','text','rank']
    return [dict(zip(keys,row)) for row in rows]
