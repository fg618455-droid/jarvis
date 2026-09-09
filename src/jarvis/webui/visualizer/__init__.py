"""🎭 Face: the live reading the control centre's face is drawn from.

``state.py`` derives the face's ``idle|listening|thinking|speaking`` reading
from the runtime phase and TTS playback that already exist, so nothing here
writes a signal file or opens a second port. The face itself is drawn by the
control centre in ``static/js/face.js``.
"""
