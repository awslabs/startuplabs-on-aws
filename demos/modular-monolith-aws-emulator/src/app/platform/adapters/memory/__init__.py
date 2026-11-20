"""In-memory adapters. Same ports, no network: unit tests and offline development.

They are also the "legacy implementation" side of a strangler fig at adapter
level: swap one port at a time from memory/legacy to AWS in `app.container`.
"""
