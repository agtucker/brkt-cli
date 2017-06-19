from asciimatics.effects import Matrix, Print
from asciimatics.particles import ShootScreen
from asciimatics.renderers import FigletText, Rainbow, SpeechBubble
from asciimatics.scene import Scene


def get_scenes(screen):
    scenes = []

    # First scene: title page
    effects = [
        Print(screen,
              Rainbow(screen, FigletText("BRKT Entertainment System",
                                         font="big")),
              y=screen.height // 2,
              transparent=False),
        Matrix(screen),
        Print(screen,
              SpeechBubble("Press SPACE to continue..."),
              screen.height - 10,
              transparent=False,
              start_frame=20)
    ]
    scenes.append(Scene(effects, -1))

    # Next scene: just dissolve the title.
    effects = [
        ShootScreen(screen, screen.width // 2, screen.height // 2, 100),
    ]
    scenes.append(Scene(effects, 10, clear=False))

    return scenes
