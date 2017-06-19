from __future__ import division

import time
from copy import deepcopy
from random import shuffle

from asciimatics.effects import Effect
from asciimatics.event import KeyboardEvent
from asciimatics.exceptions import NextScene
from asciimatics.renderers import Box, FigletText, StaticRenderer
from asciimatics.scene import Scene
from asciimatics.screen import Screen

import brkt_cli.game
from brkt_cli.game.log_streamer import LogStreamer

I_BLOCK = [
    [0, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 0, 0]
]
L_BLOCK = [
    [0, 0, 0, 0],
    [0, 0, 1, 0],
    [1, 1, 1, 0],
    [0, 0, 0, 0]
]
J_BLOCK = [
    [0, 0, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 1],
    [0, 0, 0, 0]
]
S_BLOCK = [
    [0, 0, 0, 0],
    [0, 1, 1, 0],
    [1, 1, 0, 0],
    [0, 0, 0, 0]
]
Z_BLOCK = [
    [0, 0, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 0]
]
T_BLOCK = [
    [0, 0, 0, 0],
    [0, 1, 0, 0],
    [1, 1, 1, 0],
    [0, 0, 0, 0]
]
O_BLOCK = [
    [0, 0, 0, 0],
    [0, 1, 1, 0],
    [0, 1, 1, 0],
    [0, 0, 0, 0]
]
TEST_BLOCK = [
    [1, 1, 1, 1],
    [1, 1, 1, 1],
    [1, 1, 1, 1],
    [1, 1, 1, 1]
]

LEFT = 'left'
RIGHT = 'right'
UP = 'up'
DOWN = 'down'
BLOCK_MATRIX_WIDTH_HEIGHT = 4
TETRIS_WIDTH = 10
TETRIS_HEIGHT = 20
BLOCKS_TO_DISPLAY_IN_QUEUE = 4
DISTANCE_FROM_TOP = 6


class Block(object):
    def __init__(self, block_type):
        # self.matrix represents the current rotation of the block, see
        # default rotations above
        self.matrix = deepcopy(block_type)
        # self.position is represented using a (x,y) tuple
        self.position = [int(TETRIS_WIDTH / 2 - BLOCK_MATRIX_WIDTH_HEIGHT / 2),
                         -1]

    def move(self, direction):
        if direction == LEFT:
            self.position[0] -= 1
        elif direction == RIGHT:
            self.position[0] += 1
        elif direction == DOWN:
            self.position[1] += 1
        else:
            raise RuntimeError('Illegal movement: %s' % direction)

    def rotate(self, clockwise=True):
        if clockwise:
            self.matrix = zip(*self.matrix[::-1])
        else:
            self.matrix = zip(*self.matrix)[::-1]


class Tetris(object):
    """
    The boards state is stored in the renderer, we just handle actions here as
    well as keep track of our moving piece, until it becomes part of the
    board (is placed)
    """

    TICK_RATE = 0.5

    def __init__(self):
        self.start_new_game()
        self.block = None
        self.block_queue = None
        self.score = None
        self.last_tick = None
        self.board = None
        self.game_is_over = False

    def start_new_game(self):
        # The board has 'height' number of rows and 'width' number of colums
        self.block = None
        self.block_queue = []
        self.score = 0
        self.last_tick = time.time()
        self.board = [[0 for _ in range(TETRIS_WIDTH)] for __ in
                      range(TETRIS_HEIGHT)]
        self.spawn_block()
        self.game_is_over = False

    def spawn_block(self):
        self.block = Block(block_type=self.get_new_block_type())
        if not self.is_legal_state():
            self.game_is_over = True

    def get_new_block_type(self):
        # We use 7 bag randomization for this
        if not self.block_queue or \
                        len(self.block_queue) <= BLOCKS_TO_DISPLAY_IN_QUEUE:
            blocks = [I_BLOCK,
                      L_BLOCK,
                      J_BLOCK,
                      S_BLOCK,
                      Z_BLOCK,
                      T_BLOCK,
                      O_BLOCK]
            shuffle(blocks)
            self.block_queue.extend(blocks)

        return self.block_queue.pop(0)

    def rotate_block(self):
        # for now we only rotate clockwise, but there's support for
        # counter-clockwise in blocks rotate function
        old_matrix = deepcopy(self.block.matrix)
        self.block.rotate()
        if not self.is_legal_state():
            self.block.matrix = old_matrix

    def move_block(self, direction):
        old_position = self.block.position[:]
        self.block.move(direction=direction)
        if not self.is_legal_state():
            self.block.position = old_position
            if direction == DOWN:
                self.freeze_block()
            return False
        return True

    def drop_block(self):
        # slams the piece as far down from the current position as possible
        while self.move_block(direction=DOWN):
            pass

    def simplify_board(self):
        for row_ind, row in enumerate(self.board):
            if all(row):
                self.board[1:row_ind + 1] = self.board[:row_ind]
                self.board[0] = [0 for _ in range(TETRIS_WIDTH)]

    def freeze_block(self):
        self.board = self.get_board()
        self.simplify_board()
        self.spawn_block()
        self.score += 1

    def is_legal_state(self):
        # Checks if the current state of the block is legal with the current
        # state of the board
        for y_index, y_list in enumerate(self.block.matrix):
            for x_index, block_value in enumerate(y_list):
                x, y = self.block.position
                board_x_pos = x + x_index
                board_y_pos = y + y_index
                if board_y_pos < 0:
                    board_value = 0
                elif board_x_pos < 0 or board_x_pos > TETRIS_WIDTH - 1 \
                        or board_y_pos > TETRIS_HEIGHT - 1:
                    board_value = 1
                else:
                    board_value = self.board[board_y_pos][board_x_pos]

                if board_value and block_value:
                    return False
        return True

    def tick(self):
        self.maybe_tick_downwards()

    def get_board(self):
        """
        Gets a representation of the board that has the block baked into it,
        ready to be rendered!
        """
        view_board = deepcopy(self.board)
        x, y = self.block.position

        for y_index, y_list in enumerate(self.block.matrix):
            for x_index, block_value in enumerate(y_list):
                # Check that it is a position on the board
                new_x = x + x_index
                new_y = y + y_index
                if not block_value:
                    continue
                if TETRIS_WIDTH > new_x > -1 and TETRIS_HEIGHT > new_y > -1:
                    view_board[new_y][new_x] = block_value

        return view_board

    def maybe_tick_downwards(self):
        now = time.time()
        time_since_last_tick = now - self.last_tick
        if time_since_last_tick > self.TICK_RATE:
            self.last_tick = now
            self.move_block(direction=DOWN)


class TetrisBoard(Effect):
    def __init__(self, screen, block_renderer, bg=Screen.COLOUR_BLACK,
                 **kwargs):
        """
        :param screen: The Screen being used for the Scene.
        :param block_renderer: The renderer for a single block piece
        :param bg: The default background colour to use for the text.

        Also see the common keyword arguments in :py:obj:`.Effect`.
        """
        super(TetrisBoard, self).__init__(**kwargs)
        self._screen = screen
        self._block_renderer = block_renderer
        self._bg = bg
        self.logical_representation = Tetris()

    def reset(self):
        self.logical_representation.start_new_game()

    @property
    def block_dimensions(self):
        image, colors = self._block_renderer.rendered_text
        block_width = max([len(row) for row in image])
        block_height = len([row for row in image if row.strip()])
        return block_width, block_height

    def _draw_box(self, x_start, y_start, width, height):
        image, colors = Box(width, height,
                            uni=self._screen.unicode_aware).rendered_text
        for (i, line) in enumerate(image):
            self._screen.paint(line, x_start, y_start + i, Screen.COLOUR_WHITE,
                               transparent=True,
                               bg=self._bg,
                               colour_map=colors[i])

    def _render_board(self):
        width, height = self.block_dimensions
        width = width * len(self.logical_representation.board[0]) + 2
        height = height * len(self.logical_representation.board) + 2
        self._draw_box(x_start=0, y_start=DISTANCE_FROM_TOP,
                       width=width, height=height)

        image, colors = self._block_renderer.rendered_text
        block_width, block_height = self.block_dimensions

        x = 1
        y = DISTANCE_FROM_TOP + 1
        for row in self.logical_representation.get_board():
            for pos in row:
                for (i, line) in enumerate(image):
                    if not pos:
                        line = " " * len(line)
                    self._screen.paint(line,
                                       x,
                                       y + i,
                                       Screen.COLOUR_WHITE,
                                       bg=self._bg,
                                       colour_map=colors[i])
                x += block_width
            x = 1
            y += block_height

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

    def _draw_text(self, x, y, text):
        image, colors = StaticRenderer(images=[text]).rendered_text
        for (i, line) in enumerate(image):
            self._screen.paint(line,
                               x, y + i,
                               Screen.COLOUR_WHITE,
                               transparent=True,
                               bg=self._bg,
                               colour_map=colors[i])

    def _draw_tetris_block(self, block_type, x_start, y_start):
        y_offset = y_start
        image, colors = self._block_renderer.rendered_text
        for row_index, row in enumerate(block_type):
            x_offset = x_start
            for value in row:
                for (i, line) in enumerate(image):
                    if not value:
                        line = " " * len(line)
                    self._screen.paint(line,
                                       x_offset,
                                       y_offset,
                                       Screen.COLOUR_WHITE,
                                       bg=self._bg,
                                       colour_map=colors[i])
                x_offset += 2
            y_offset += 1

    def _draw_tetris_blocks(self, x_start, y_start):
        block_queue = self.logical_representation.block_queue[
                      :BLOCKS_TO_DISPLAY_IN_QUEUE]
        for block_type in block_queue:
            self._draw_tetris_block(block_type=block_type,
                                    x_start=x_start, y_start=y_start)
            y_start += 4

    def _render_sidebar(self):
        # Common params for both bars
        block_width, block_height = self.block_dimensions
        x_start = block_width * len(self.logical_representation.board[0]) + 2
        width = block_width + 10

        # Draw score bar
        y_start = DISTANCE_FROM_TOP
        height = block_height * 2 + 1
        self._draw_box(x_start=x_start, y_start=y_start, width=width,
                       height=height)
        text = str(self.logical_representation.score)
        text_x_start = x_start + int(width / 2 + 1) - len(text)
        self._draw_text(x=text_x_start, y=y_start + 1, text=text)

        # Draw queue bar
        y_start += height
        height = (block_height * 4) * BLOCKS_TO_DISPLAY_IN_QUEUE + 3
        self._draw_box(x_start=x_start, y_start=y_start, width=width,
                       height=height)
        self._draw_tetris_blocks(x_start=x_start + 2, y_start=y_start + 1)
        return

    def _render_title(self):
        image, _ = FigletText('TETRIS', font='big').rendered_text
        self._draw_image(image, 0, 0)

    def _update(self, frame_no):
        self.logical_representation.tick()
        if self.logical_representation.game_is_over:
            brkt_cli.game.game_score = {
                'score': self.logical_representation.score,
                'game': 'tetris'
            }
            raise NextScene("Game_Over")
        self._render_title()
        self._render_board()
        self._render_sidebar()

    @property
    def stop_frame(self):
        return 0

    def process_event(self, event):
        if isinstance(event, KeyboardEvent):
            key = event.key_code
            if key == 32:
                self.logical_representation.drop_block()
            elif key == Screen.KEY_UP:
                self.logical_representation.rotate_block()
            elif key == Screen.KEY_DOWN:
                self.logical_representation.move_block(DOWN)
            elif key == Screen.KEY_LEFT:
                self.logical_representation.move_block(LEFT)
            elif key == Screen.KEY_RIGHT:
                self.logical_representation.move_block(RIGHT)
            elif key == Screen.KEY_ESCAPE:
                raise NextScene("Main_Menu")
        else:
            return event


def get_scenes(screen):
    scenes = []

    # MAIN GAME
    effects = [
        TetrisBoard(
                screen,
                StaticRenderer(images=['[]'])
        ),
        LogStreamer(
                screen,
                0,
                screen.height - 3)
    ]
    scenes.append(Scene(effects, -1, name="Tetris_Game"))

    return scenes
