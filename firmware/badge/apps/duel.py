"""Quick Draw: a two-player reaction duel on the LED bar.

Button A and button B are the two players. The bar counts down in red, goes
dark for a random time, then flashes GREEN. First press wins the round.
Pressing early loses it. Wins light up the bar from each end (A from LED 0,
B from LED 7). First to WIN_SCORE takes the match.
"""

import json
import random
import time

from apps.base_app import BaseApp
from lib.button import UserA, UserB
from lib.epd import display
from lib.led_bar import NUM_LEDS, leds
from lib import pixtext

POSTER = "/data/graphics/duel.png"
SCORES_FILE = "/data/duel_scores.json"
WIN_SCORE = 4
HOF_SIZE = 5
MIN_VALID_MS = 100  # anything faster is a lucky guess, not a reaction

OFF = (0, 0, 0)
RED = (3, 0, 0)
GREEN = (0, 3, 0)
GOLD = (3, 2, 0)
IDLE = (0, 1, 1)

# States
WAIT_DISPLAY, READY, COUNT, HOLD, GO, RESULT, MATCH = range(7)

# Countdown: LEDs lit (inner pair first) at each step, in ms
_COUNT_STEPS = (0, 500, 1000, 1500)
_PAIRS = ((3, 4), (2, 5), (1, 6), (0, 7))


class DuelApp(BaseApp):
    def __init__(self, name: str | None = None) -> None:
        self.name = "Quick Draw"
        self.sleep_ms = 5
        self.launched = False
        self.state = WAIT_DISPLAY
        self.t0 = 0
        self.scores = [0, 0]
        self.hold_ms = 0
        self.last_frame = None
        self.winner = 0
        self.times = [[], []]      # reaction times this match, per player
        self.match_new = []        # hall-of-fame entries set this match
        self.hof = None            # [[ms, "A"/"B"], ...] fastest ever, sorted

    # ---- fastest-ever list (kept in flash) -------------------------------
    def _load_hof(self):
        if self.hof is None:
            try:
                with open(SCORES_FILE) as f:
                    self.hof = json.load(f)["best"]
            except (OSError, ValueError, KeyError):
                self.hof = []
        return self.hof

    def _save_hof(self):
        try:
            with open(SCORES_FILE, "w") as f:
                json.dump({"best": self.hof}, f)
        except OSError as ex:
            print("Quick Draw: could not save scores", ex)

    def _note_time(self, player, ms):
        self.times[player].append(ms)
        if ms >= MIN_VALID_MS:
            hof = self._load_hof()
            entry = [ms, "AB"[player]]
            hof.append(entry)
            hof.sort(key=lambda e: e[0])
            del hof[HOF_SIZE:]
            if any(e is entry for e in hof):
                self.match_new.append(entry)

    def _draw_result(self):
        """Scoreboard for the e-paper after a match."""
        w = self.winner
        black, red = display.black, display.red
        display.clear()
        red.fill_rect(0, 0, 960, 16, 1)
        red.fill_rect(0, 656, 960, 16, 1)
        pixtext.center(black, "QUICK DRAW", 34, 6)
        pixtext.center(
            red, "%s WINS %d-%d" % ("AB"[w], self.scores[w], self.scores[1 - w]), 110, 10
        )
        allt = [(ms, "AB"[p]) for p in (0, 1) for ms in self.times[p]]
        if allt:
            ms, who = min(allt)
            pixtext.center(black, "BEST DRAW %d MS (%s)" % (ms, who), 215, 5)
        pixtext.center(black, "FASTEST EVER", 285, 5)
        black.fill_rect(240, 330, 480, 4, 1)
        hof = self._load_hof()
        y = 350
        for rank in range(HOF_SIZE):
            if rank < len(hof):
                e = hof[rank]
                line = "%d. %d MS  %s" % (rank + 1, e[0], e[1])
                new = any(e is n for n in self.match_new)
                if new:
                    line += "  NEW!"
            else:
                line, new = "%d. ---" % (rank + 1), False
            pixtext.center(red if new else black, line, y, 5)
            y += 50
        pixtext.center(black, "PRESS A OR B TO PLAY AGAIN", 618, 3)
        display.update()

    # ---- helpers ---------------------------------------------------------
    def _elapsed(self):
        return time.ticks_diff(time.ticks_ms(), self.t0)

    def _goto(self, state):
        self.state = state
        self.t0 = time.ticks_ms()

    def _show(self, frame):
        if frame != self.last_frame:
            for i, c in enumerate(frame):
                leds[i] = c
            leds.write()
            self.last_frame = frame

    def _score_frame(self, background):
        frame = [background] * NUM_LEDS
        for i in range(self.scores[0]):
            frame[i] = GOLD
        for i in range(self.scores[1]):
            frame[NUM_LEDS - 1 - i] = GOLD
        return tuple(frame)

    def _half(self, player, color, other=OFF):
        half = NUM_LEDS // 2
        frame = [other] * NUM_LEDS
        start = 0 if player == 0 else half
        for i in range(start, start + half):
            frame[i] = color
        return tuple(frame)

    def _clear_presses(self):
        UserA.pressed()
        UserB.pressed()

    def _round_over(self, winner, reaction=None, why=""):
        self.winner = winner
        self.scores[winner] += 1
        if reaction is not None:
            self._note_time(winner, reaction)
        if reaction is None:
            print("Quick Draw: player %s wins (%s)" % ("AB"[winner], why))
        else:
            print("Quick Draw: player %s wins in %d ms" % ("AB"[winner], reaction))
        self._goto(MATCH if self.scores[winner] >= WIN_SCORE else RESULT)

    # ---- app entry -------------------------------------------------------
    def run(self):
        if self.launched:
            self.scores = [0, 0]
            self.times = [[], []]
            self.match_new = []
            self.last_frame = None
            display.clear()
            display.draw_png(POSTER)
            display.update()
            self._goto(WAIT_DISPLAY)
            return

        s = self.state
        if s == WAIT_DISPLAY:
            # The refresh animation owns the LEDs until the panel is done
            if self._elapsed() > 500 and display.is_done_updating():
                self._clear_presses()
                self._goto(READY)
        elif s == READY:
            blink = (self._elapsed() // 400) % 2
            self._show(self._score_frame(IDLE if blink else OFF))
            if UserA.pressed() or UserB.pressed():
                self._clear_presses()
                self._goto(COUNT)
        elif s == COUNT:
            t = self._elapsed()
            frame = [OFF] * NUM_LEDS
            for step, at in enumerate(_COUNT_STEPS):
                if t >= at:
                    for i in _PAIRS[step]:
                        frame[i] = RED
            self._show(tuple(frame))
            if UserA.pressed():
                self._round_over(1, why="A jumped the gun")
            elif UserB.pressed():
                self._round_over(0, why="B jumped the gun")
            elif t >= _COUNT_STEPS[-1] + 500:
                self.hold_ms = random.randint(1000, 4000)
                self._goto(HOLD)
        elif s == HOLD:
            self._show((OFF,) * NUM_LEDS)
            if UserA.pressed():
                self._round_over(1, why="A jumped the gun")
            elif UserB.pressed():
                self._round_over(0, why="B jumped the gun")
            elif self._elapsed() >= self.hold_ms:
                self._clear_presses()
                self._goto(GO)
        elif s == GO:
            self._show((GREEN,) * NUM_LEDS)
            a = UserA.pressed()
            b = UserB.pressed()
            if a or b:
                if a and b:
                    winner = 0 if UserA.pressed_ms() >= UserB.pressed_ms() else 1
                else:
                    winner = 0 if a else 1
                btn = UserA if winner == 0 else UserB
                reaction = max(0, self._elapsed() - btn.pressed_ms())
                self._round_over(winner, reaction)
            elif self._elapsed() > 3000:
                print("Quick Draw: nobody pressed, replay")
                self._goto(READY)
        elif s == RESULT:
            t = self._elapsed()
            on = (t // 200) % 2 == 0
            self._show(self._half(self.winner, GREEN if on else OFF, other=RED))
            if t > 1400:
                self._clear_presses()
                self._goto(READY)
        elif s == MATCH:
            t = self._elapsed()
            # Victory chase in the winner's colours over the whole bar
            pos = (t // 80) % NUM_LEDS
            frame = [OFF] * NUM_LEDS
            frame[pos] = GOLD
            frame[(pos - 1) % NUM_LEDS] = GREEN
            frame[(pos - 2) % NUM_LEDS] = (0, 1, 0)
            self._show(tuple(frame))
            if t > 3500:
                self._save_hof()
                self._draw_result()
                self.scores = [0, 0]
                self.times = [[], []]
                self.match_new = []
                self.last_frame = None
                self._clear_presses()
                self._goto(WAIT_DISPLAY)
