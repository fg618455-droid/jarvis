"""🎭 Face/visualizer view: live state bridge plus the vendored face gallery.

``vendor/`` holds the AGPL-3.0-licensed ai-visualizer files, unmodified apart
from pointing their two fetch calls at this control centre's own API instead
of ai-visualizer's own stdlib server. ``state.py`` is Jarvis's own code: it
derives the face's ``idle|listening|thinking|speaking`` reading from the
runtime phase and TTS playback that already exist, so nothing here writes a
signal file or opens a second port.
"""
