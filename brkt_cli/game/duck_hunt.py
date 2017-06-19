import random
import time

from asciimatics.effects import Effect, Sprite
from asciimatics.event import KeyboardEvent, MouseEvent
from asciimatics.exceptions import NextScene
from asciimatics.particles import Explosion, Rain
from asciimatics.paths import DynamicPath, Path
from asciimatics.renderers import SpeechBubble, StaticRenderer
from asciimatics.scene import Scene
from asciimatics.screen import Screen
from asciimatics.sprites import Arrow

import brkt_cli.game
from log_streamer import LogStreamer

MAX_MISSED_DUCKS = 5


class DuckHuntStats(object):
    missed_ducks = 0
    hit_ducks = 0


class MouseController(DynamicPath):
    def __init__(self, sprite, screen, x, y):
        super(MouseController, self).__init__(screen, x, y)
        self._sprite = sprite

    def process_event(self, event):
        if isinstance(event, MouseEvent):
            self._x = event.x
            self._y = event.y
            if event.buttons & MouseEvent.DOUBLE_CLICK != 0:
                # Try to whack the other sprites when mouse is clicked
                self._sprite.shoot(powerful=True)
            elif event.buttons & MouseEvent.LEFT_CLICK != 0:
                # Try to whack the other sprites when mouse is clicked
                self._sprite.shoot(powerful=False)
            else:
                return event
        else:
            return event


class Gun(Sprite):
    def __init__(self, screen):
        """
        See :py:obj:`.Sprite` for details.
        """
        super(Gun, self).__init__(
                screen,
                renderer_dict={
                    "default": StaticRenderer(images=["X"])
                },
                path=MouseController(
                        self, screen, screen.width // 2, screen.height // 2),
                colour=Screen.COLOUR_BLACK)
        self._screen = screen

    def shoot(self, powerful=True):
        x, y = self._path.next_pos()
        for duck in [e for e in self._scene.effects if isinstance(e, Duck)]:
            try:
                if self.overlaps(duck, use_new_pos=True):
                    duck.get_shot()
            except TypeError:
                # Probably compared against an unitialized sprite
                pass
        if powerful:
            self._scene.add_effect(Explosion(self._screen, x, y, 25))
        else:
            #TODO(Adam): Does the same for now which is kinda lame
            self._scene.add_effect(Explosion(self._screen, x, y, 25))

class Duck(Arrow):
    def __init__(self, screen, path):
        super(Duck, self).__init__(screen, path)

    def _update(self, frame_no):
        last_y_pos = self.last_position()[1]
        if last_y_pos is not None and last_y_pos < -5:
            DuckHuntStats.missed_ducks += 1
            self.delete_count = 1
        super(Duck, self)._update(frame_no)

    def get_shot(self):
        DuckHuntStats.hit_ducks += 1
        self.delete_count = 1


class DuckSpawner(Effect):
    def __init__(self, screen, spawn_rate):
        """
        Spawns ducks with random paths
        :param screen: screen
        :param spawn_rate: delay between new ducks
        """
        super(DuckSpawner, self).__init__()
        self._screen = screen
        self.spawn_rate = spawn_rate
        self.last_spawn_time = 0

    def spawn_duck(self):
        path = Path()
        path.jump_to(self._screen.width * random.randint(0, 1),
                     self._screen.height)
        target = int(self._screen.width * random.random())
        path.move_straight_to(target, -10, 100)
        self._scene.add_effect(Duck(self._screen, path))
        self.last_spawn_time = time.time()

    def _update(self, frame_no):
        if DuckHuntStats.missed_ducks >= MAX_MISSED_DUCKS:
            brkt_cli.game.game_score = {
                'score': DuckHuntStats.hit_ducks,
                'game': 'duck_hunt'
            }
            raise NextScene("Game_Over")
        if time.time() > self.last_spawn_time + self.spawn_rate:
            self.spawn_duck()

        image, _ = SpeechBubble(
                "Score: %d" % DuckHuntStats.hit_ducks).rendered_text
        for i, line in enumerate(image):
            self._screen.paint(line,
                               (self._screen.width - len(line)) // 2,
                               self._screen.height - 5 + i,
                               Screen.COLOUR_WHITE)

        image, _ = SpeechBubble(
                "Misses left: %d" % (MAX_MISSED_DUCKS -
                                     DuckHuntStats.missed_ducks,)
        ).rendered_text
        for i, line in enumerate(image):
            self._screen.paint(line,
                               (self._screen.width - len(line)) // 2,
                               self._screen.height - 3 + i,
                               Screen.COLOUR_WHITE)

    def process_event(self, event):
        if isinstance(event, KeyboardEvent):
            key = event.key_code
            if key == Screen.KEY_ESCAPE:
                raise NextScene("Main_Menu")
            else:
                # Consume all keyboard presses
                pass
        else:
            return event

    def reset(self):
        super(DuckSpawner, self).reset()
        DuckHuntStats.missed_ducks = 0
        DuckHuntStats.hit_ducks = 0

    @property
    def stop_frame(self):
        return self._stop_frame


def get_scenes(screen):
    scenes = []

    # MAIN GAME
    effects = [
        DuckSpawner(screen, 1),
        Gun(screen),
        Rain(
                screen,
                9999999
        ),
        LogStreamer(
                screen,
                0,
                screen.height - 3)

    ]
    scenes.append(Scene(effects, -1, name="Duck_Hunt_Game"))

    return scenes
