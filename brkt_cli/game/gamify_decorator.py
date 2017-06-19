import logging
import multiprocessing
import os
import sys
import time

from brkt_cli import brkt_env_from_values

has_asciimatics = True
try:
    import asciimatics
    assert asciimatics
except ImportError:
    has_asciimatics = False

if has_asciimatics:
    import brkt_cli.game as game
    import game_controller


def redirect_stream_logging():
    root = logging.getLogger()
    formatter = None
    new_handler = None

    original_handlers = []
    for handler in root.handlers[:]:
        if isinstance(handler, logging.StreamHandler):
            original_handlers.append(handler)
            formatter = handler.formatter
            root.removeHandler(handler)
    if formatter:
        new_handler = logging.FileHandler(game.TMP_LOG_FILE)
        logging.getLogger('asciimatics').setLevel(logging.FATAL)
        logging.getLogger('brkt_cli.game').setLevel(logging.FATAL)
        new_handler.setFormatter(formatter)
        root.addHandler(new_handler)
    return original_handlers, new_handler


def restore_stream_logging(original_handlers, new_handler):
    root = logging.getLogger()
    root.removeHandler(new_handler)
    for handler in original_handlers:
        root.addHandler(handler)


def print_all_new_lines(log_file):
    while True:
        new_data = log_file.readline().strip()
        if new_data:
            print new_data
        else:
            break


def gamify(func):
    """Decorator that let's you play a game while the underlying function is
    being executed"""

    def func_wrapper(*args, **kwargs):
        if not args or not args[0].fun:
            return func(*args, **kwargs)

        if not has_asciimatics:
            logging.error("The BRKT Entertainment System requires the"
                          "asciimatics library. Please run:\n"
                          "pip install asciimatics==1.8.0")
            return 1

        try:
            brkt_env = brkt_env_from_values(args[0], args[1])
            game.yeti_env = 'https://api.%s' % (
                '.'.join(brkt_env.public_api_host.split('.')[1:]))
            game.token = os.getenv('BRKT_API_TOKEN', None)
        except Exception as e:
            logging.error("You can play but high scores aren't available. "
                          "Error: %s", e)

        logging.info("Starting BRKT Entertainment System")
        time.sleep(1)

        def special_func(*inargs, **inkwargs):
            """
            This replaces any StreamHandler with a FileHandler with the
            same format. If no streamhandler exists we ignore logging and
            just pipe all stdout and stdin to a file
            """
            sys.stdout = sys.stderr = open(game.TMP_LOG_FILE, 'w')
            func(*inargs, **inkwargs)

        original_handlers, new_handler = redirect_stream_logging()
        p_cli = multiprocessing.Process(target=special_func,
                                        args=args,
                                        kwargs=kwargs)
        p_cli.start()

        p_game = multiprocessing.Process(target=game_controller.main)
        p_game.start()

        # We want to let the new process create the file before trying to
        # read it. Kinda glitchy :(
        time.sleep(1)

        with open(game.TMP_LOG_FILE) as log_file:
            while True:
                if p_game.exitcode is not None:
                    print_all_new_lines(log_file)
                if p_cli.exitcode is not None and p_game.exitcode is not None:
                    break
                time.sleep(0.1)

        os.remove(game.TMP_LOG_FILE)

        restore_stream_logging(original_handlers, new_handler)

        return p_cli.exitcode

    return func_wrapper
