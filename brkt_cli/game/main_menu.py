from __future__ import division

from asciimatics.effects import Effect
from asciimatics.event import KeyboardEvent
from asciimatics.exceptions import NextScene, StopApplication
from asciimatics.renderers import FigletText, Rainbow
from asciimatics.scene import Scene
from asciimatics.screen import Screen

from brkt_cli.game import ENTER
from log_streamer import LogStreamer


class GameSelector(Effect):
    def __init__(self, screen, games, y, **kwargs):
        super(GameSelector, self).__init__(**kwargs)
        self.games = games
        self.games.append(("EXIT", "exit"))
        self.x = 3
        self.y = y
        self.selection = 0
        self._screen = screen
        self._header = Rainbow(self._screen,
                               FigletText("BRKT ENTERTAINMENT SYSTEM",
                                          font='big')
                               ).rendered_text
        self._game_images = [FigletText(game, font='doom').rendered_text for
                             (game, _) in games]
        self._arrow = FigletText('>', font='doom').rendered_text

    def reset(self):
        self.selection = 0

    def _draw_image(self, image, x, y, color=Screen.COLOUR_WHITE,
                    colour_map=None):
        for (i, line) in enumerate(image):
            cmap = colour_map[i] if colour_map else None
            self._screen.paint(line,
                               x,
                               y + i,
                               color,
                               bg=Screen.COLOUR_BLACK,
                               transparent=False,
                               colour_map=cmap)

    def _draw_arrow(self, color):
        image, _ = self._arrow
        y = self.y + 10 + self.selection * len(self._game_images[0][0])
        self._draw_image(image, self.x, y, color)

    def move_selection(self, up):
        self._draw_arrow(Screen.COLOUR_BLACK)
        self.selection += -1 if up else 1
        if self.selection < 0:
            self.selection = len(self.games) - 1
        if self.selection >= len(self.games):
            self.selection = 0
        self._draw_arrow(Screen.COLOUR_RED)

    def _update(self, frame_no):

        self._draw_arrow(Screen.COLOUR_RED)
        x = self.x
        y = self.y

        x += len(self._arrow[0][0]) + 2
        image, colour_map = self._header
        self._draw_image(image, x, y, Screen.COLOUR_WHITE,
                         colour_map=colour_map)
        y += 10

        for i in range(len(self.games)):
            image, _ = self._game_images[i]
            if i == self.selection:
                color = Screen.COLOUR_RED
            else:
                color = Screen.COLOUR_WHITE
            # print color
            self._draw_image(image, x, y, color)
            y += len(image)

    @property
    def stop_frame(self):
        return self._stop_frame

    def process_event(self, event):
        if isinstance(event, KeyboardEvent):
            key = event.key_code
            if key == Screen.KEY_UP:
                self.move_selection(up=True)
            elif key == Screen.KEY_DOWN:
                self.move_selection(up=False)
            elif key == ENTER:
                if self.games[self.selection][1] == 'exit':
                    raise StopApplication("User initated exit")
                raise NextScene(self.games[self.selection][1])
            else:
                # Consume output
                pass
        else:
            return event


def get_scenes(screen):
    effects = [
        # Matrix(
        #         screen,
        #         # 1000
        # ),
        GameSelector(
                screen,
                [
                    ("TETRIS", "Tetris_Game"),
                    ("DUCK HUNT", "Duck_Hunt_Game")
                ],
                screen.height // 6),
        LogStreamer(
                screen,
                0,
                screen.height - 3)
    ]
    return [Scene(effects, -1, name='Main_Menu')]
