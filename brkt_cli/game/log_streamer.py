from asciimatics.effects import Effect
from asciimatics.screen import Screen

from brkt_cli.game import TMP_LOG_FILE


def tail(f, lines=20):
    total_lines_wanted = lines

    BLOCK_SIZE = 1024
    f.seek(0, 2)
    block_end_byte = f.tell()
    lines_to_go = total_lines_wanted
    block_number = -1
    blocks = []  # blocks of size BLOCK_SIZE, in reverse order starting
    # from the end of the file
    while lines_to_go > 0 and block_end_byte > 0:
        if (block_end_byte - BLOCK_SIZE > 0):
            # read the last block we haven't yet read
            f.seek(block_number * BLOCK_SIZE, 2)
            blocks.append(f.read(BLOCK_SIZE))
        else:
            # file too small, start from begining
            f.seek(0, 0)
            # only read what was not read
            blocks.append(f.read(block_end_byte))
        lines_found = blocks[-1].count('\n')
        lines_to_go -= lines_found
        block_end_byte -= BLOCK_SIZE
        block_number -= 1
    all_read_text = ''.join(reversed(blocks))
    return all_read_text.splitlines()[-total_lines_wanted:]


class LogStreamer(Effect):
    def __init__(self, screen, x, y, **kwargs):
        super(LogStreamer, self).__init__(**kwargs)
        self.x = x
        self.y = y
        self._screen = screen

    def reset(self):
        pass

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

        with open(TMP_LOG_FILE) as log_file:
            lines = tail(log_file, 3)
            if lines:
                self._draw_image(lines, self.x, self.y)

    @property
    def stop_frame(self):
        return self._stop_frame
