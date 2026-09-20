"""Publishing what was said in a Discord thread into its GitHub item's comment section.

The only part of this project that reads Discord rather than writing to it. `log` owns which
threads are being captured and holds the messages, `flush` decides when to publish them, and
`publish` turns lines into a comment.
"""
