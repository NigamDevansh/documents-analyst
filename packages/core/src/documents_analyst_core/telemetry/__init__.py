"""Cost and stage accounting.

Core produces telemetry values; it never persists them. Persistence needs
SQLAlchemy, and core imports none — so the application supplies a Sink.
"""
