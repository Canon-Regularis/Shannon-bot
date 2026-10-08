"""`/remind`: a ping somebody asked for, sent once when its time comes. Issue #229.

Two halves, split the way the transcript publisher is: `book` writes a reminder down when the
command is run, and `send` reads it back when it falls due. They share the table and nothing else,
so either can be read without the other.
"""
