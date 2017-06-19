from asciimatics.effects import Effect, Print
from asciimatics.event import KeyboardEvent
from asciimatics.exceptions import NextScene
from asciimatics.renderers import Figlet, FigletText, Fire
from asciimatics.scene import Scene
from asciimatics.screen import Screen

import brkt_cli.game
from brkt_cli.yeti import post_json
from log_streamer import LogStreamer


class ScoreReporter(Effect):
    def __init__(self, screen, y, **kwargs):
        super(ScoreReporter, self).__init__(**kwargs)
        self.x = 5
        self.y = y
        self.selection = 0
        self._screen = screen
        self.score_reported = False
        self.name = ""

    def reset(self):
        self.score_reported = False
        self.name = ""

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

    def _update(self, frame_no):
        game_score = brkt_cli.game.game_score
        game_name = game_score['game'] if game_score else 'Unknown'
        image, _ = FigletText(game_name, font='small').rendered_text
        self._draw_image(image, self.x, self.y)

        score = game_score['score'] if game_score else -1
        image, _ = FigletText("Score: %s" % score, font='small').rendered_text
        self._draw_image(image, self.x, self.y + 5)

        name = "Enter name: %s" % (self.name,)
        image, _ = FigletText(name, font='small').rendered_text
        self._draw_image(image, self.x, self.y + 10)

    @property
    def stop_frame(self):
        return self._stop_frame

    def process_event(self, event):
        if isinstance(event, KeyboardEvent):
            key = event.key_code
            if key == Screen.KEY_ESCAPE:
                raise NextScene("Main_Menu")
            if key == brkt_cli.game.BACKSPACE:
                self.name = self.name[:-1]
            elif key == brkt_cli.game.ENTER:
                self.report_score()
                raise NextScene("Main_Menu")
            elif key == brkt_cli.game.SPACE:
                pass
            else:
                try:
                    self.name += chr(key)
                except ValueError:
                    pass
        else:
            return event

    def report_score(self):
        if not (brkt_cli.game.yeti_env and brkt_cli.game.token) or \
                self.score_reported:
            return

        game_score = brkt_cli.game.game_score
        if 'game' not in game_score or 'score' not in game_score:
            return

        payload = {
            'game': game_score['game'],
            'name': self.name,
            'score': game_score['score']
        }

        try:
            post_json(
                    brkt_cli.game.yeti_env + '/api/v1/game/score',
                    token=brkt_cli.game.token,
                    json=payload
            )
            self.score_reported = True
        except Exception:
            pass


def get_scenes(screen):
    scenes = []

    text = Figlet(font="banner", width=200).renderText("GAME OVER")
    width = max([len(x) for x in text.split("\n")])
    effects = [
        Print(screen,
              Fire(screen.height, 100, text, 0.4, 60, screen.colours),
              y=0,
              speed=1,
              transparent=False),
        Print(screen,
              FigletText("GAME OVER", "banner"),
              y=screen.height // 2 - 9,
              x=(screen.width - width) // 2 + 1,
              colour=Screen.COLOUR_BLACK,
              bg=Screen.COLOUR_BLACK,
              speed=1),
        Print(screen,
              FigletText("GAME OVER", "banner"),
              y=screen.height // 2 - 9,
              x=(screen.width - width) // 2,
              colour=Screen.COLOUR_WHITE,
              bg=Screen.COLOUR_WHITE,
              speed=1),
        ScoreReporter(screen=screen,
                      y=0),
        LogStreamer(
                screen,
                0,
                screen.height - 3)
    ]
    scenes.append(Scene(effects, -1, name="Game_Over"))

    return scenes
