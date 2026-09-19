"""Publishing what was said in a Discord thread into its GitHub item's comment section.

Issue #103, and the only thing in this project that reads Discord rather than writing to it.

`lines` is the currency: a `TranscriptLine` and the rendering of a list of them, both pure and
knowing nothing about tables. `publish` turns lines into a comment. `log` owns which threads are
being captured and holds the messages until they are wanted, and `flush` decides when that is.

The split exists so the flusher is one producer of lines rather than the only one. A later
command that picks individual messages out of a thread chooses its lines differently and hands
them to the same publisher.
"""
