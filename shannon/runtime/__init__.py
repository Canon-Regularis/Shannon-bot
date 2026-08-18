"""Owning the process: what runs beside the API, and how it starts and stops.

Kept apart from `main`, which now only assembles the app and hands it to uvicorn. These pieces
have their own tests and none of them knows what a webhook is.
"""
