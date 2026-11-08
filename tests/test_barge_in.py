"""Ticket ea6c257f: "When I talk, you stop." Jarvis pauses as soon as there's a voice on the line, and stops once words confirm it."""
import asyncio

import numpy as np

from core.voice import Interrupter, Mouth, VoiceDetector

RNG = np.random.default_rng(7)
STEP = 0.01


def frame(level, sec=STEP):
    n = int(48000 * sec)
    return np.clip(RNG.normal(0, level, n), -32000, 32000).astype(np.int16).tobytes()


class FakeMouth:
    def __init__(self, playing=True):
        self.playing, self.paused, self.stops = playing, False, 0

    busy = property(lambda s: s.playing)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.stops += 1
        self.playing, self.paused = False, False


class Line:
    """Feeds the interrupter level by level on a fake clock, the way a call does."""
    def __init__(self, mouth=None):
        self.mouth = mouth or FakeMouth()
        self.it = Interrupter(self.mouth, self.mouth.stop)
        self.t = 100.0

    def play(self, level, sec):
        for _ in range(int(sec / STEP)):
            self.it.heard(frame(level), self.t)
            self.it.tick(self.t)
            self.t += STEP
            if self.mouth.paused and getattr(self, "paused_at", None) is None:
                self.paused_at = self.t
        return self


def test_detector_ignores_line_noise_and_clicks():
    v = VoiceDetector()
    assert not any(v.feed(frame(50)) for _ in range(200))
    assert not any(v.feed(frame(4000)) for _ in range(5))                     # 50 ms click


def test_detector_hears_a_voice_in_about_a_fifth_of_a_second_and_when_it_ends():
    v = VoiceDetector()
    for _ in range(100):
        v.feed(frame(50))
    results = [v.feed(frame(3000)) for _ in range(40)]
    assert results.index(True) in range(18, 24)
    assert v.talking and any(not v.feed(frame(50)) for _ in range(70))


def test_voice_pauses_jarvis_at_once_and_keeps_what_it_was_saying():
    line = Line().play(40, 1.0)
    assert not line.mouth.paused
    start = line.t
    line.play(3000, 0.3)
    assert line.mouth.paused and line.paused_at - start < 0.3 and line.mouth.stops == 0


def test_words_confirm_it_and_the_reply_is_dropped():
    line = Line().play(40, 1.0).play(3000, 0.5)
    line.it.words("can you", line.t + 0.5)
    assert line.mouth.stops == 1 and not line.mouth.paused


def test_a_noise_with_no_words_lets_jarvis_carry_on():
    line = Line().play(40, 1.0).play(3000, 0.3)
    assert line.mouth.paused
    line.play(40, 1.5)
    assert not line.mouth.paused and line.mouth.stops == 0


def test_the_same_noise_doesnt_pause_it_again():
    line = Line().play(40, 1.0).play(3000, 0.3).play(40, 1.5)
    line.paused_at = None
    line.play(1900, 0.5)
    assert line.paused_at is None                                             # below what the false alarm taught it


def test_a_bang_doesnt_make_it_deaf_to_him():
    line = Line().play(40, 1.0).play(30000, 0.3).play(40, 1.5)
    assert line.it.vad.echo <= VoiceDetector.MAX_ECHO
    line.paused_at = None
    line.play(5000, 0.3)
    assert line.paused_at is not None


def test_jarvis_echo_is_learned_not_taken_for_him():
    line = Line().play(1000, 3.0)                                             # its own voice coming back, from the first frame
    assert not line.mouth.paused


def test_he_still_gets_through_over_the_echo():
    line = Line().play(500, 2.0)
    line.play(4000, 0.4)
    assert line.mouth.paused


def test_nothing_happens_while_jarvis_is_silent():
    line = Line(FakeMouth(playing=False)).play(3000, 0.5)
    assert not line.mouth.paused and line.mouth.stops == 0


def test_it_carries_on_if_the_audio_stops_coming_while_paused():
    line = Line().play(40, 1.0).play(3000, 0.3)
    line.it.tick(line.t + 0.9)                                                # he muted: no more frames, only the clock
    assert not line.mouth.paused


def test_a_sound_that_never_ends_doesnt_hold_jarvis_forever():
    line = Line().play(40, 1.0).play(3000, Interrupter.MAX_PAUSE + 0.5)
    assert not line.mouth.paused or line.t - line.it.paused_at < Interrupter.MAX_PAUSE


def test_his_sentence_stops_jarvis_the_original_bug():
    line = Line()
    assert line.it.sentence("Can you stop here?") is True and line.mouth.stops == 1


def test_a_bit_of_echo_while_jarvis_is_audible_is_ignored():
    line = Line()
    assert line.it.sentence("Ладно.") is False and line.mouth.stops == 0


def test_a_short_answer_counts_once_jarvis_has_paused_for_it():
    line = Line().play(40, 1.0).play(3000, 0.3)
    assert line.it.sentence("Stop.") is True and line.mouth.stops == 1


def test_a_sentence_when_jarvis_is_quiet_just_passes():
    line = Line(FakeMouth(playing=False))
    assert line.it.sentence("Ok") is True and line.mouth.stops == 0


def test_three_words_stop_it_even_without_a_pause_as_before():
    line = Line()
    line.it.words("can you stop", line.t)
    assert line.mouth.stops == 1
    line = Line()
    line.it.words("hello", line.t)
    assert line.mouth.stops == 0


def test_words_right_after_the_pause_were_said_before_it():
    line = Line().play(40, 1.0).play(3000, 0.25)
    line.it.words("two words", line.it.paused_at + 0.1)
    assert line.mouth.stops == 0


def test_the_real_mouth_keeps_its_audio_while_paused_and_drops_it_on_stop():
    async def go():
        m = Mouth()
        m.out.extend(b"\x01" * 100)
        m.pause()
        assert m.paused and m.playing and len(m.out) == 100
        m.resume()
        assert not m.paused and len(m.out) == 100
        m.pause()
        m.stop()
        assert not m.paused and not m.out
        await m.close()
    asyncio.run(go())
